// This file is part of Indico.
// Copyright (C) 2002 - 2025 CERN
//
// Indico is free software; you can redistribute it and/or
// modify it under the terms of the MIT License; see the
// LICENSE file for more details.

import * as actions from './actions';

const initialState = {
  entries: [],
  keyword: null,
  currentPage: 1,
  isFetching: false,
  metadataQuery: {},
  filters: {},
  pages: [],
  totalPageCount: 0,
  currentViewIndex: null,
};

export default function logReducer(state = initialState, action) {
  switch (action.type) {
    case actions.SET_INITIAL_REALMS:
      return {...state, filters: Object.fromEntries(action.initialRealms.map(r => [r, true]))};
    case actions.SET_KEYWORD:
      return {...state, keyword: action.keyword};
    case actions.SET_FILTER:
      return {...state, filters: {...state.filters, ...action.filter}};
    case actions.SET_PAGE:
      return {...state, currentPage: action.currentPage};
    case actions.UPDATE_ENTRIES:
      return {
        ...state,
        entries: action.entries,
        pages: action.pages,
        totalPageCount: action.totalPageCount,
        isFetching: false,
      };
    case actions.FETCH_STARTED:
      return {...state, isFetching: true};
    case actions.FETCH_FAILED:
      return {...state, isFetching: false};
    case actions.SET_DETAILED_VIEW:
      return {...state, currentViewIndex: action.currentViewIndex};
    case actions.SET_METADATA_QUERY:
      return {...state, metadataQuery: action.metadataQuery};
    default:
      return state;
  }
}
