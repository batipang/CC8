"""Content-free scores for a fixed corpus; never records, transcribes or executes."""

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import unicodedata

CATEGORIES = frozenset(('qa', 'names_places', 'numbers_time', 'mixed', 'pause_soft'))
PARTITIONS = ('calibration', 'acceptance')
CONDITIONS = ('quiet', 'tv')
DISTANCES = ('1m', '3m')
CORPUS = Path(__file__).parent / 'resources/asr-corpus-v1.json'
MAX_EDIT_CELLS = 5_000_000


def rejected(*_):
    raise ValueError('Invalid evaluation JSON')


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            rejected()
        result[key] = value
    return result


def read_json(path, *, private=False):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size > 512 * 1024
            or private
            and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077)
        ):
            rejected()
        raw = stream.read(512 * 1024 + 1)
    if len(raw) > 512 * 1024:
        rejected()
    return json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object, parse_constant=rejected)


def text(value, maximum):
    if not isinstance(value, str) or len(value) > maximum:
        rejected()
    if any(unicodedata.category(char) in ('Cc', 'Cs') for char in value):
        rejected()
    return value


def normalized(value):
    value = unicodedata.normalize('NFKC', value).casefold()
    return ''.join(
        char
        for index, char in enumerate(value)
        if char.isalnum()
        or char in '+-−%/='
        or char in '.:'
        and 0 < index < len(value) - 1
        and value[index - 1].isdigit()
        and value[index + 1].isdigit()
    )


def normalized_field(value):
    # Manual numeric annotations must keep signs, decimal points and units.
    return ''.join(
        char for char in unicodedata.normalize('NFKC', value).casefold() if not char.isspace()
    )


def han(value):
    return [
        char
        for char in unicodedata.normalize('NFKC', value)
        if '\u3400' <= char <= '\u9fff' or '\U00020000' <= char <= '\U000323af'
    ]


def words(value):
    return re.findall(
        r"[a-z]+[0-9]*(?:['-][a-z]+)*", unicodedata.normalize('NFKC', value).casefold()
    )


def edit_counts(reference, hypothesis):
    # Bounded two-row Levenshtein; retain S/D/I, prefer diagonal on equal cost.
    previous = [(i, 0, 0, i) for i in range(len(hypothesis) + 1)]
    for index, expected in enumerate(reference, 1):
        current = [(index, 0, index, 0)]
        for column, actual in enumerate(hypothesis, 1):
            cost, substitutions, deletions, insertions = previous[column - 1]
            diagonal = (
                cost + (expected != actual),
                substitutions + (expected != actual),
                deletions,
                insertions,
            )
            cost, substitutions, deletions, insertions = previous[column]
            deletion = (cost + 1, substitutions, deletions + 1, insertions)
            cost, substitutions, deletions, insertions = current[-1]
            insertion = (cost + 1, substitutions, deletions, insertions + 1)
            current.append(min((diagonal, deletion, insertion), key=lambda item: item[0]))
        previous = current
    errors, substitutions, deletions, insertions = previous[-1]
    return {
        'reference_units': len(reference),
        'errors': errors,
        'substitutions': substitutions,
        'deletions': deletions,
        'insertions': insertions,
    }


def corpus_samples(data):
    if (
        not isinstance(data, dict)
        or set(data) != {'schema', 'corpus', 'samples'}
        or type(data['schema']) is not int
        or data['schema'] != 1
        or data['corpus'] != 'cc8-generic-v1'
    ):
        rejected()
    rows = data['samples']
    if not isinstance(rows, list) or not 1 <= len(rows) <= 75:
        rejected()
    result = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            'id',
            'category',
            'partition',
            'delivery',
            'reference',
            'critical_fields',
        }:
            rejected()
        ident = text(row['id'], 40)
        if (
            not re.fullmatch(r'[a-z_]+-[0-9]{2}', ident)
            or ident in result
            or row['category'] not in CATEGORIES
            or row['partition'] not in PARTITIONS
            or row['delivery'] not in ('normal', 'pause_or_soft')
            or not text(row['reference'], 180)
        ):
            rejected()
        fields = row['critical_fields']
        if not isinstance(fields, dict) or len(fields) > 8:
            rejected()
        for key, variants in fields.items():
            if (
                not re.fullmatch(r'[a-z_]{1,24}', key)
                or not isinstance(variants, list)
                or not 1 <= len(variants) <= 4
                or any(not text(value, 80) or not normalized(value) for value in variants)
            ):
                rejected()
        result[ident] = row
    return result


def metadata(data):
    if (
        not isinstance(data, dict)
        or set(data) != {'schema', 'engine', 'version', 'source', 'samples'}
        or type(data['schema']) is not int
        or data['schema'] != 1
    ):
        rejected()
    if (
        data['engine'] not in ('minimax', 'sensevoice', 'whisper', 'unspecified')
        or data['source'] not in ('human', 'synthetic', 'unspecified')
        or not isinstance(data['version'], str)
        or not re.fullmatch(r'[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}', data['version'])
    ):
        rejected()
    return {key: data[key] for key in ('engine', 'version', 'source')}


def score_rows(data, corpus):
    labels = metadata(data)
    if not isinstance(data['samples'], list) or len(data['samples']) > len(corpus) * 4:
        rejected()
    rows = {}
    seen = set()
    cells = 0
    for row in data['samples']:
        if (
            not isinstance(row, dict)
            or set(row)
            - {'id', 'condition', 'distance', 'outcome', 'transcript', 'fields', 'intent_correct'}
            or not {'id', 'condition', 'distance', 'outcome', 'transcript'} <= set(row)
        ):
            rejected()
        ident = text(row['id'], 40)
        if (
            ident not in corpus
            or row['condition'] not in CONDITIONS
            or row['distance'] not in DISTANCES
            or row['outcome'] not in ('recognized', 'failed', 'not_detected', 'unmeasured')
        ):
            rejected()
        key = (ident, row['condition'], row['distance'])
        transcript = text(row['transcript'], 1000)
        observed = row.get('fields', {})
        expected = corpus[ident]
        fields = expected['critical_fields']
        if (
            key in seen
            or not isinstance(observed, dict)
            or set(observed) - set(fields)
            or any(not isinstance(value, str) or not text(value, 80) for value in observed.values())
            or ('intent_correct' in row and type(row['intent_correct']) is not bool)
            or row['outcome'] != 'recognized'
            and (transcript or observed or row.get('intent_correct'))
        ):
            rejected()
        seen.add(key)
        if row['outcome'] == 'unmeasured':
            continue
        han_reference, han_observed = han(expected['reference']), han(transcript)
        word_reference, word_observed = words(expected['reference']), words(transcript)
        cells += len(han_reference) * len(han_observed) + len(word_reference) * len(word_observed)
        if cells > MAX_EDIT_CELLS:
            rejected()
        annotated = len(observed)
        correct = sum(
            normalized_field(value) in {normalized_field(option) for option in fields[name]}
            for name, value in observed.items()
        )
        rows[key] = {
            'id': ident,
            'condition': row['condition'],
            'distance': row['distance'],
            'partition': expected['partition'],
            'category': expected['category'],
            'outcome': row['outcome'],
            'chinese_han_cer': edit_counts(han_reference, han_observed),
            'english_wer': edit_counts(word_reference, word_observed),
            'transcript_exact': normalized(expected['reference']) == normalized(transcript),
            'fields_expected': len(fields),
            'fields_annotated': annotated,
            'field_names_annotated': sorted(observed),
            'fields_correct': correct,
            'critical_sample_annotated': bool(fields) and annotated == len(fields),
            'critical_sample_exact': bool(fields)
            and annotated == len(fields)
            and correct == len(fields),
            'intent_annotated': 'intent_correct' in row,
            'intent_correct': row.get('intent_correct', False),
        }
    return labels, rows


def aggregate(rows, planned):
    result = {
        'planned': planned,
        'observed': len(rows),
        'missing': planned - len(rows),
        'outcomes': dict(Counter(row['outcome'] for row in rows)),
    }
    for metric in ('chinese_han_cer', 'english_wer'):
        values = {
            field: sum(row[metric][field] for row in rows)
            for field in ('reference_units', 'errors', 'substitutions', 'deletions', 'insertions')
        }
        values['rate'] = (
            values['errors'] / values['reference_units'] if values['reference_units'] else None
        )
        result[metric] = values
    for field in (
        'transcript_exact',
        'fields_expected',
        'fields_annotated',
        'fields_correct',
        'critical_sample_annotated',
        'critical_sample_exact',
        'intent_annotated',
        'intent_correct',
    ):
        result[field] = sum(row[field] for row in rows)
    result['critical_sample_exact_rate'] = (
        result['critical_sample_exact'] / result['critical_sample_annotated']
        if result['critical_sample_annotated']
        else None
    )
    result['intent_accuracy'] = (
        result['intent_correct'] / result['intent_annotated']
        if result['intent_annotated']
        else None
    )
    return result


def report(labels, rows, corpus):
    groups = []
    for partition in PARTITIONS:
        for condition in CONDITIONS:
            for distance in DISTANCES:
                selected = [
                    row
                    for row in rows.values()
                    if (row['partition'], row['condition'], row['distance'])
                    == (partition, condition, distance)
                ]
                planned = sum(sample['partition'] == partition for sample in corpus.values())
                groups.append(
                    {
                        'partition': partition,
                        'condition': condition,
                        'distance': distance,
                        **aggregate(selected, planned),
                    }
                )
    return {
        'labels': labels,
        'labels_are_operator_supplied': True,
        'groups': groups,
        'categories': {
            partition: {
                category: aggregate(
                    [
                        row
                        for row in rows.values()
                        if row['category'] == category and row['partition'] == partition
                    ],
                    sum(
                        row['category'] == category and row['partition'] == partition
                        for row in corpus.values()
                    )
                    * 4,
                )
                for category in sorted(CATEGORIES)
            }
            for partition in PARTITIONS
        },
        'cases': list(rows.values()),
    }


def evaluate(corpus_data, candidate_data, baseline_data=None):
    corpus = corpus_samples(corpus_data)
    labels, candidate = score_rows(candidate_data, corpus)
    result = {
        'schema': 1,
        'contains_content': False,
        'human_acceptance_asserted': False,
        'metrics_only': True,
        'corpus_sha256': hashlib.sha256(
            json.dumps(
                corpus_data, ensure_ascii=False, sort_keys=True, separators=(',', ':')
            ).encode()
        ).hexdigest(),
        'candidate': report(labels, candidate, corpus),
        'comparison': None,
    }
    if baseline_data is not None:
        baseline_labels, baseline = score_rows(baseline_data, corpus)
        if set(baseline) != set(candidate):
            raise ValueError('A/B comparison requires identical sample and scenario coverage')
        if any(
            (baseline[key]['field_names_annotated'], baseline[key]['intent_annotated'])
            != (candidate[key]['field_names_annotated'], candidate[key]['intent_annotated'])
            for key in candidate
        ):
            raise ValueError('A/B comparison requires matched annotation coverage')
        if baseline_labels['source'] != labels['source']:
            raise ValueError('A/B comparison requires the same declared source class')
        result['baseline'] = report(baseline_labels, baseline, corpus)
        deltas = []
        for before, after in zip(result['baseline']['groups'], result['candidate']['groups']):
            delta = {key: after[key] for key in ('partition', 'condition', 'distance')}
            for metric in ('chinese_han_cer', 'english_wer'):
                a, b = after[metric]['rate'], before[metric]['rate']
                delta[metric + '_delta'] = a - b if a is not None and b is not None else None
            a, b = after['critical_sample_exact_rate'], before['critical_sample_exact_rate']
            delta['critical_sample_exact_rate_delta'] = (
                a - b if a is not None and b is not None else None
            )
            deltas.append(delta)
        result['comparison'] = {
            'paired_cases': len(candidate),
            'deltas_candidate_minus_baseline': deltas,
        }
    return result


def make_template(corpus_data):
    samples = corpus_samples(corpus_data)
    return {
        'schema': 1,
        'engine': 'unspecified',
        'version': '0.0.0',
        'source': 'unspecified',
        'samples': [
            {
                'id': ident,
                'condition': condition,
                'distance': distance,
                'outcome': 'unmeasured',
                'transcript': '',
                'fields': {},
            }
            for ident in samples
            for condition in CONDITIONS
            for distance in DISTANCES
        ],
    }
