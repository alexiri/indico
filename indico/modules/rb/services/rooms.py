# -*- coding: utf-8 -*-
##
##
## This file is part of Indico.
## Copyright (C) 2002 - 2013 European Organization for Nuclear Research (CERN).
##
## Indico is free software; you can redistribute it and/or
## modify it under the terms of the GNU General Public License as
## published by the Free Software Foundation; either version 3 of the
## License, or (at your option) any later version.
##
## Indico is distributed in the hope that it will be useful, but
## WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
## General Public License for more details.
##
## You should have received a copy of the GNU General Public License
## along with Indico;if not, see <http://www.gnu.org/licenses/>.

from flask import session

from indico.core.config import Config
from indico.modules.rb.controllers.mixins import RoomBookingAvailabilityParamsMixin
from indico.modules.rb.models.blockings import Blocking
from indico.modules.rb.models.locations import Location
from indico.modules.rb.models.rooms import Room
from MaKaC.services.implementation.base import LoggedOnlyService, ServiceBase
from MaKaC.services.interface.rpc.common import ServiceError


class RoomBookingListRooms(ServiceBase):
    def _checkParams(self):
        try:
            self._location = Location.getLocationByName(self._params['location'])
        except:
            raise ServiceError('ERR-RB0', 'Invalid location name: {0}.'.format(self._params['location']))

    def _getAnswer(self):
        return dict((room.name, room.name)
                    for room in self._location.getRoomsOrderedByNames())


class RoomBookingFullNameListRooms(RoomBookingListRooms):
    def _getAnswer(self):
        return dict((room.name, room.getFullName())
                    for room in self._location.getRoomsOrderedByNames())


# TODO
class RoomBookingAvailabilitySearchRooms(ServiceBase, RoomBookingAvailabilityParamsMixin):
    def _checkParams(self):
        try:
            self._location = self._params["location"]
        except:
            raise ServiceError("ERR-RB0", "Invalid location.")

        self._checkParamsRepeatingPeriod(self._params)

    def _getAnswer(self):
        p = ReservationBase()
        p.startDT = self._startDT
        p.endDT = self._endDT
        p.repeatability = self._repeatability

        rooms = CrossLocationQueries.getRooms(location=self._location, resvExample=p, available=True)

        return [room.id for room in rooms]


# TODO:
class RoomBookingListLocationsAndRooms(ServiceBase):
    def _getAnswer(self):
        if not Config.getInstance().getIsRoomBookingActive():
            return []
        result = {}
        locationNames = map(lambda l: l.friendlyName, Location.allLocations)
        for loc in locationNames:
            for room in CrossLocationQueries.getRooms(location=loc):
                result[loc + ":" + room.name] = loc + ":" + room.name
        return sorted(result)


class RoomBookingListLocationsAndRoomsWithGuids(ServiceBase):
    def _checkParams(self):
        self._isActive = self._params.get('isActive', None)

    def _getAnswer(self):
        if not Config.getInstance().getIsRoomBookingActive():
            return {}
        criteria = {'_eager': Room.location}
        if self._isActive is not None:
            criteria['is_active'] = self._isActive
        rooms = Room.find_all(**criteria)
        return {room.id: '{}: {}'.format(room.location_name, room.getFullName()) for room in rooms}


# Refactored from GetBookingBase
# TODO:
class RoomBookingLocationRoomAddress(object):
    def _getRoomInfo(self, target):
        location = target.getOwnLocation()

        if location:
            locName = location.getName()
            locAddress = location.getAddress()
        else:
            locName = None
            locAddress = None

        room = target.getOwnRoom()

        if room:
            roomName = room.getName()
        else:
            roomName = None

        return {
            'location': locName,
            'room': roomName,
            'address': locAddress
        }

    def _getAnswer(self):
        return self._getRoomInfo(self._target)


# TODO
class RoomBookingLocationsAndRoomsGetLink(ServiceBase):
    def _checkParams(self):
        self._location = self._params["location"]
        self._room = self._params["room"]

    def _getAnswer(self):
        return linking.RoomLinker().getURLByName(self._room, self._location)


class BookingPermission(LoggedOnlyService):
    def _checkParams(self):
        blocking_id = self._params.get('blocking_id')
        self._room = Room.get(self._params['room_id'])
        self._blocking = Blocking.get(blocking_id) if blocking_id else None

    def _getAnswer(self):
        user = session.user
        return {
            'blocked': not self._blocking.can_be_overridden(user, self._room) if self._blocking else False,
            'can_book': self._room.can_be_booked(user) or self._room.can_be_prebooked(user),
            'group': self._room.get_attribute_value('allowed-booking-group')
        }
