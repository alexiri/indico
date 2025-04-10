# This file is part of Indico.
# Copyright (C) 2002 - 2025 CERN
#
# Indico is free software; you can redistribute it and/or
# modify it under the terms of the MIT License; see the
# LICENSE file for more details.

import csv
from collections import defaultdict
from datetime import timedelta
from operator import attrgetter

import dateutil.parser
from flask import session
from sqlalchemy.orm import contains_eager, joinedload, load_only, noload

from indico.core.config import config
from indico.core.db import db
from indico.core.errors import UserValueError
from indico.modules.attachments.util import get_attached_items
from indico.modules.events.abstracts.settings import BOASortField
from indico.modules.events.contributions.models.contributions import Contribution
from indico.modules.events.contributions.models.persons import (AuthorType, ContributionPersonLink,
                                                                SubContributionPersonLink)
from indico.modules.events.contributions.models.principals import ContributionPrincipal
from indico.modules.events.contributions.models.subcontributions import SubContribution
from indico.modules.events.contributions.operations import create_contribution
from indico.modules.events.models.events import Event
from indico.modules.events.models.persons import EventPerson
from indico.modules.events.persons.util import get_event_person
from indico.modules.events.timetable.models.entries import TimetableEntry
from indico.modules.events.util import track_time_changes
from indico.util.date_time import format_human_timedelta
from indico.util.i18n import _
from indico.util.spreadsheets import csv_text_io_wrapper
from indico.util.string import format_email_with_name, validate_email
from indico.web.flask.templating import get_template_module
from indico.web.flask.util import send_file, url_for
from indico.web.forms.base import IndicoForm
from indico.web.forms.fields.simple import make_keywords_field
from indico.web.util import jsonify_data


def get_events_with_linked_contributions(user, dt=None):
    """
    Return a dict with keys representing event_id and the values containing
    data about the user rights for contributions within the event.

    :param user: A `User`
    :param dt: Only include events taking place on/after that date
    """
    def add_acl_data():
        query = (user.in_contribution_acls
                 .options(load_only('contribution_id', 'permissions', 'full_access', 'read_access'))
                 .options(noload('*'))
                 .options(contains_eager(ContributionPrincipal.contribution).load_only('event_id'))
                 .join(Contribution)
                 .join(Event, Event.id == Contribution.event_id)
                 .filter(~Contribution.is_deleted, ~Event.is_deleted, Event.ends_after(dt)))
        for principal in query:
            roles = data[principal.contribution.event_id]
            if 'submit' in principal.permissions:
                roles.add('contribution_submission')
            if principal.full_access:
                roles.add('contribution_manager')
            if principal.read_access:
                roles.add('contribution_access')

    def add_contrib_data():
        has_contrib = (EventPerson.contribution_links.any(
            ContributionPersonLink.contribution.has(~Contribution.is_deleted)))
        has_subcontrib = EventPerson.subcontribution_links.any(
            SubContributionPersonLink.subcontribution.has(db.and_(
                ~SubContribution.is_deleted,
                SubContribution.contribution.has(~Contribution.is_deleted))))
        query = (Event.query
                 .options(load_only('id'))
                 .options(noload('*'))
                 .filter(~Event.is_deleted,
                         Event.ends_after(dt),
                         Event.persons.any((EventPerson.user_id == user.id) & (has_contrib | has_subcontrib))))
        for event in query:
            data[event.id].add('contributor')

    data = defaultdict(set)
    add_acl_data()
    add_contrib_data()
    return data


def sort_contribs(contribs, sort_by):
    mapping = {'number': 'friendly_id', 'name': 'title'}
    if sort_by == BOASortField.schedule:
        key_func = lambda c: (c.start_dt is None, c.start_dt)
    elif sort_by == BOASortField.session_title:
        key_func = lambda c: (c.session is None, c.session.title.lower() if c.session else '')
    elif sort_by == BOASortField.speaker:
        def key_func(c):
            speakers = c.speakers
            if not c.speakers:
                return True, None
            return False, speakers[0].get_full_name(last_name_upper=False, abbrev_first_name=False).lower()
    elif sort_by == BOASortField.board_number:
        key_func = attrgetter('board_number')
    elif sort_by == BOASortField.session_board_number:
        key_func = lambda c: (c.session is None, c.session.title.lower() if c.session else '', c.board_number)
    elif sort_by == BOASortField.schedule_board_number:
        key_func = lambda c: (c.start_dt is None, c.start_dt, c.board_number or '')
    elif sort_by == BOASortField.session_schedule_board:
        key_func = lambda c: (c.session is None, c.session.title.lower() if c.session else '',
                              c.start_dt is None, c.start_dt, c.board_number or '')
    elif sort_by == BOASortField.id:
        key_func = attrgetter('friendly_id')
    elif sort_by == BOASortField.title:
        key_func = attrgetter('title')
    elif isinstance(sort_by, str) and sort_by:
        key_func = attrgetter(mapping.get(sort_by) or sort_by)
    else:
        key_func = attrgetter('title')
    return sorted(contribs, key=key_func)


def _names_with_emails(person_links):
    return [format_email_with_name(x.full_name, x.email) for x in person_links if x.email]


def generate_spreadsheet_from_contributions(contributions):
    """
    Return a tuple consisting of spreadsheet columns and respective
    contribution values.
    """
    has_board_number = any(c.board_number for c in contributions)
    has_authors = any(pl.author_type != AuthorType.none for c in contributions for pl in c.person_links)
    headers = ['Id', 'Title', 'Description', 'Date', 'Duration', 'Type', 'Session', 'Track', 'Presenters',
               'Presenters (affiliation)', 'Presenters (email)', 'Materials', 'Program Code']
    if has_authors:
        headers += ['Authors', 'Authors (affiliation)', 'Authors (email)',
                    'Co-Authors', 'Co-Authors (affiliation)', 'Co-Authors (email)']
    if has_board_number:
        headers.append('Board number')
    rows = []
    for c in sort_contribs(contributions, sort_by='friendly_id'):
        contrib_data = {'Id': c.friendly_id, 'Title': c.title, 'Description': c.description,
                        'Duration': format_human_timedelta(c.duration),
                        'Date': c.timetable_entry.start_dt if c.timetable_entry else None,
                        'Type': c.type.name if c.type else None,
                        'Session': c.session.title if c.session else None,
                        'Track': c.track.title if c.track else None,
                        'Materials': None,
                        'Presenters': ', '.join(speaker.full_name for speaker in c.speakers),
                        'Presenters (affiliation)': ', '.join(speaker.full_name_affiliation for speaker in c.speakers),
                        'Presenters (email)': ', '.join(_names_with_emails(c.speakers)),
                        'Program Code': c.code}
        if has_authors:
            contrib_data.update({
                'Authors': ', '.join(author.full_name for author in c.primary_authors),
                'Authors (affiliation)': ', '.join(author.full_name_affiliation for author in c.primary_authors),
                'Authors (email)': ', '.join(_names_with_emails(c.primary_authors)),
                'Co-Authors': ', '.join(author.full_name for author in c.secondary_authors),
                'Co-Authors (affiliation)': ', '.join(author.full_name_affiliation for author in c.secondary_authors),
                'Co-Authors (email)': ', '.join(_names_with_emails(c.secondary_authors)),
            })
        if has_board_number:
            contrib_data['Board number'] = c.board_number

        attached_items = get_attached_items(c, preload_event=(len(contributions) > 10))
        attachments = [att.absolute_download_url for att in attached_items.get('files', [])]
        attachments.extend(attachment.absolute_download_url
                           for folder in attached_items.get('folders', [])
                           for attachment in folder.attachments)

        if attachments:
            contrib_data['Materials'] = ', '.join(attachments)
        rows.append(contrib_data)
    return headers, rows


def make_contribution_form(event, *, contrib=None, management=True, only_custom_fields=False):
    """Extend the contribution WTForm to add the extra fields.

    Each extra field will use a field named ``custom_ID`` (except `keywords`).

    :param event: The `Event` for which to create the contribution form.
    :return: A `ContributionForm` subclass.
    """
    from indico.modules.events.contributions.forms import ContributionForm

    base_class = IndicoForm if only_custom_fields else ContributionForm
    form_class = type('_ContributionForm', (base_class,), {})
    if not only_custom_fields:
        form_class.keywords = make_keywords_field('contribution', contrib.keywords if contrib else [])
    for custom_field in event.contribution_fields:
        field_impl = custom_field.mgmt_field if management else custom_field.field
        if field_impl is None:
            # field definition is not available anymore
            continue
        if not custom_field.is_active or (not management and not custom_field.is_user_editable):
            continue
        name = f'custom_{custom_field.id}'
        setattr(form_class, name, field_impl.create_wtf_field())
    return form_class


def contribution_type_row(contrib_type):
    template = get_template_module('events/contributions/management/_types_table.html')
    html = template.types_table_row(contrib_type=contrib_type)
    return jsonify_data(html_row=html, flash=False)


def _query_contributions_with_user_as_submitter(event, user):
    return (Contribution.query.with_parent(event)
            .filter(Contribution.acl_entries.any(db.and_(ContributionPrincipal.has_management_permission('submit'),
                                                         ContributionPrincipal.user == user))))


def get_contributions_with_user_as_submitter(event, user):
    """Get a list of contributions in which the `user` has submission rights."""
    return (_query_contributions_with_user_as_submitter(event, user)
            .options(joinedload('acl_entries'))
            .order_by(db.func.lower(Contribution.title))
            .all())


def has_contributions_with_user_as_submitter(event, user):
    return _query_contributions_with_user_as_submitter(event, user).has_rows()


def get_contributions_for_person(event, person, only_speakers=False):
    """Get all contributions for an event person.

    If ``only_speakers`` is true, then only contributions where the person is a
    speaker are returned
    """
    cl_join = db.and_(ContributionPersonLink.person_id == person.id,
                      ContributionPersonLink.contribution_id == Contribution.id)

    if only_speakers:
        cl_join &= ContributionPersonLink.is_speaker

    return (Contribution.query
            .with_parent(event)
            .join(ContributionPersonLink, cl_join)
            .outerjoin(TimetableEntry)
            .order_by(TimetableEntry.start_dt, db.func.lower(Contribution.title), Contribution.friendly_id)
            .all())


def _query_contributions_for_user(event, user):
    """Query for all contributions in an event associated with the given user.

    The query looks for contributions where the user is a primary/secondary author, speaker or a submitter.
    """
    condition = db.or_(EventPerson.user == user,
                       Contribution.acl_entries.any(db.and_(ContributionPrincipal.has_management_permission('submit'),
                                                            ContributionPrincipal.user == user)))
    return (Contribution.query.with_parent(event)
            .outerjoin(ContributionPersonLink)  # outer join in case there is a contribution with
            .outerjoin(EventPerson)             # no person links but the user has submission rights.
            .filter(condition))


def user_has_contributions(event, user):
    """Return True if a user has any contributions in the given event."""
    return _query_contributions_for_user(event, user).has_rows()


def get_contributions_for_user(event, user):
    """Get all contributions for a user in the given event."""
    return _query_contributions_for_user(event, user).all()


def serialize_contribution_for_ical(contrib):
    from indico.modules.events.persons.schemas import PersonLinkSchema
    return {
        '_fossil': 'contributionMetadata',
        'id': contrib.id,
        'startDate': contrib.timetable_entry.start_dt if contrib.timetable_entry else None,
        'endDate': contrib.timetable_entry.end_dt if contrib.timetable_entry else None,
        'url': url_for('contributions.display_contribution', contrib, _external=True),
        'title': contrib.title,
        'location': contrib.venue_name,
        'roomFullname': contrib.room_name,
        'speakers': [PersonLinkSchema().dump(x) for x in contrib.speakers],
        'description': contrib.description
    }


def import_contributions_from_csv(event, f, delimiter=','):
    """Import timetable contributions from a CSV file into an event."""
    with csv_text_io_wrapper(f) as ftxt:
        reader = csv.reader(ftxt.read().splitlines(), delimiter=delimiter)

    contrib_data = []
    for num_row, row in enumerate(reader, 1):
        try:
            start_dt, duration, title, first_name, last_name, affiliation, email = (value.strip() for value in row)
            email = email.lower()
        except ValueError:
            raise UserValueError(_('Row {}: malformed CSV data - please check that the number of columns is correct')
                                 .format(num_row))
        try:
            parsed_start_dt = event.tzinfo.localize(dateutil.parser.parse(start_dt)) if start_dt else None
        except ValueError:
            raise UserValueError(_('Row {row}: can\'t parse date: "{date}"').format(row=num_row, date=start_dt))

        try:
            parsed_duration = timedelta(minutes=int(duration)) if duration else None
        except ValueError:
            raise UserValueError(_("Row {row}: can't parse duration: {duration}").format(row=num_row,
                                                                                         duration=duration))

        if not title:
            raise UserValueError(_('Row {}: contribution title is required').format(num_row))

        if email and not validate_email(email):
            raise UserValueError(_('Row {row}: invalid email address: {email}').format(row=num_row, email=email))

        contrib_data.append({
            'start_dt': parsed_start_dt,
            'duration': parsed_duration or timedelta(minutes=20),
            'title': title,
            'speaker': {
                'first_name': first_name,
                'last_name': last_name,
                'affiliation': affiliation,
                'email': email
            }
        })

    # now that we're sure the data is OK, let's pre-allocate the friendly ids
    # for the contributions in question
    Contribution.allocate_friendly_ids(event, len(contrib_data))
    contributions = []
    all_changes = defaultdict(list)

    for contrib_fields in contrib_data:
        speaker_data = contrib_fields.pop('speaker')

        with track_time_changes() as changes:
            contribution = create_contribution(event, contrib_fields, extend_parent=True)

        contributions.append(contribution)
        for key, val in changes[event].items():
            all_changes[key].append(val)

        email = speaker_data['email']
        if not email:
            continue

        # set the information of the speaker
        person = get_event_person(event, speaker_data)
        link = ContributionPersonLink(person=person, is_speaker=True)
        link.populate_from_dict({
            'first_name': speaker_data['first_name'],
            'last_name': speaker_data['last_name'],
            'affiliation': speaker_data['affiliation']
        })
        contribution.person_links.append(link)

    return contributions, all_changes


def render_pdf(event, contribs, sort_by, cls):
    pdf = cls(event, session.user, contribs, tz=event.timezone, sort_by=sort_by)
    res = pdf.generate()
    return send_file('book-of-abstracts.pdf', res, 'application/pdf')


def render_archive(event, contribs, sort_by, cls):
    tex = cls(event, session.user, contribs, tz=event.timezone, sort_by=sort_by)
    archive = tex.generate_source_archive()
    return send_file('contributions-tex.zip', archive, 'application/zip', inline=False)


def get_boa_export_formats():
    formats = {'PDF': (_('PDF'), render_pdf),
               'ZIP': (_('TeX archive'), render_archive)}
    if not config.LATEX_ENABLED:
        del formats['PDF']
    return formats
