"""Pure scoped-report metadata validation, independent of readers and model clients."""

from typing import TypedDict, cast

from report_archive_protocol import token
from source_identity import identifier, title


class ScopeMember(TypedDict):
    registration_id: str
    thread_id: str
    title: str
    project: str


class ScopeMetadata(TypedDict):
    kind: str
    source_id: str
    source_title: str
    members: list[ScopeMember]


def label(value: object) -> None:
    if not isinstance(value, str) or title(value) != value:
        raise ValueError('Invalid saved report source label')
    value.encode('utf-8')


def validate(value: object) -> ScopeMetadata:
    if not isinstance(value, dict) or set(value) != {
        'kind',
        'source_id',
        'source_title',
        'members',
    }:
        raise ValueError('Invalid saved progress source')
    identifier(value['source_id'])
    label(value['source_title'])
    members = value['members']
    if not isinstance(members, list) or len(members) > 8:
        raise ValueError('Invalid saved progress members')
    if value['kind'] == 'selected' and not members:
        return cast(ScopeMetadata, value)
    if value['kind'] != 'registered' or not members:
        raise ValueError('Invalid saved progress source kind')
    ids: set[str] = set()
    threads: set[str] = set()
    for member in members:
        if not isinstance(member, dict) or set(member) != {
            'registration_id',
            'thread_id',
            'title',
            'project',
        }:
            raise ValueError('Invalid saved progress member')
        ident = token(member['registration_id'], 16)
        thread = identifier(member['thread_id'])
        label(member['title'])
        label(member['project'])
        if ident in ids or thread in threads:
            raise ValueError('Duplicate saved progress member')
        ids.add(ident)
        threads.add(thread)
    return cast(ScopeMetadata, value)
