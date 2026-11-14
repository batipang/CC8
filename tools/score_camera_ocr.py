"""Score paired CC8 camera OCR results against fixed public test cards."""

import argparse
from html import escape
import json
from math import prod
from pathlib import Path
import re
import sys
import unicodedata

if __package__:
    from .private_output import PrivateOutput, read_private_json
else:
    from private_output import PrivateOutput, read_private_json

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'cc8-assistant/host'))
from asr_evaluation import edit_counts  # noqa: E402

# Generic labels and printed text only. Test sheets should use the same text,
# layout, distance and lighting for both resolutions.
CARDS = (
    '白色马克杯',
    '蓝色签字笔',
    '纸巾盒',
    '便携手电筒',
    '玻璃水瓶',
    '型号 CC8-A27',
    '净含量 350 mL',
    '额定电压 5 V',
    '批号 A0O1I8',
    '室内湿度 45%',
    '会议时间 14:30\n地点 二楼东侧',
    '样品编号 017\n保质期 24 个月',
    '请先核对标签\n确认后再归档',
    '2026-11-14\n检查结果 合格',
    '数量 12 件\n单价 8.50 元',
    'USB-C 输入\nDC 5V 2A',
    'OPEN / 关\n向右转动',
    'Wi-Fi 2.4 GHz\n信号 正常',
    '上层：常温存放\n下层：避光存放',
    '示例 C8B\n读数 0.75 kg\n复测两次',
)
CORPUS = 'cc8-camera-ocr-v1'
MODES = ('standard', 'hd')
MAX_DIMENSIONS = {'standard': (640, 480), 'hd': (1280, 960)}
UNREADABLE = '[无法辨认]'


def cards():
    return {f'card-{index:02}': text for index, text in enumerate(CARDS, 1)}


def card_sheet():
    options = []
    panels = []
    for ident, text in cards().items():
        options.append(f'<option value="{ident}">{ident}</option>')
        panels.append(f'<section id="{ident}" class="card">{escape(text)}</section>')
    return f"""<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CC8 OCR 测试卡</title>
<style>
  :root {{ color-scheme: light; font-family: system-ui, "PingFang SC", sans-serif; }}
  body {{ margin: 0; background: white; color: black; }}
  nav {{ display: flex; gap: .75rem; align-items: center; justify-content: center;
    padding: 1rem; background: #f0f0f0; }}
  button, select {{ min-height: 2.5rem; font: inherit; }}
  .card {{ display: grid; place-content: center; box-sizing: border-box;
    width: 100%; min-height: 68vh; padding: 1rem; text-align: left;
    white-space: pre-wrap; font-size: clamp(1.25rem, 6vw, 3rem); line-height: 1.5; }}
  .card[hidden] {{ display: none; }}
  @page {{ size: A4 landscape; margin: 18mm; }}
  @media print {{
    nav {{ display: none; }}
    .card, .card[hidden] {{ display: grid; min-height: 150mm; font-size: 32pt;
      break-after: page; }}
    .card:last-child {{ break-after: auto; }}
  }}
</style>
<nav aria-label="测试卡导航">
  <button type="button" id="previous">上一张</button>
  <label>测试卡 <select id="card-picker">{''.join(options)}</select></label>
  <button type="button" id="next">下一张</button>
</nav>
<main>{''.join(panels)}</main>
<script>
  const picker = document.getElementById('card-picker');
  const previous = document.getElementById('previous');
  const next = document.getElementById('next');
  const cards = document.querySelectorAll('.card');
  function showCard() {{
    const index = picker.selectedIndex;
    cards.forEach((card, position) => {{ card.hidden = position !== index; }});
    previous.disabled = index === 0;
    next.disabled = index === cards.length - 1;
  }}
  picker.addEventListener('change', showCard);
  previous.addEventListener('click', () => {{ picker.selectedIndex--; showCard(); }});
  next.addEventListener('click', () => {{ picker.selectedIndex++; showCard(); }});
  showCard();
</script>
</html>
"""


def template(*, use_receipts=False):
    field = 'request_id' if use_receipts else 'dimensions'
    return {
        'schema': 1,
        'corpus': CORPUS,
        'samples': {
            ident: {mode: {'status': 'not_run', 'text': '', field: None} for mode in MODES}
            for ident in cards()
        },
    }


def normalize(text):
    # Keep punctuation, case, digits, and character order. Ignore visual spacing.
    return ''.join(char for char in unicodedata.normalize('NFC', text) if not char.isspace())


def receipt_index(report):
    if (
        not isinstance(report, dict)
        or report.get('ok') is not True
        or type(report.get('schema')) is not int
        or report['schema'] != 1
        or report.get('contains_content') is not False
        or not isinstance(report.get('requests'), list)
        or len(report['requests']) > 128
    ):
        raise ValueError('Invalid camera diagnostics report')
    rows = {}
    for row in report['requests']:
        if not isinstance(row, dict):
            raise ValueError('Invalid diagnostics request')
        ident = row.get('request_id')
        if not isinstance(ident, str) or not re.fullmatch('[a-f0-9]{32}', ident) or ident in rows:
            raise ValueError('Invalid diagnostics request identity')
        rows[ident] = row
    return rows


def receipt_dimensions(rows, used, ident, status):
    if status == 'not_run' and ident is None:
        return None
    if not isinstance(ident, str) or ident not in rows or ident in used:
        raise ValueError('Missing or reused OCR request receipt')
    row = rows[ident]
    if (
        row.get('origin') != 'vision'
        or row.get('camera_ocr') is not True
        or (status, row.get('outcome')) not in (('recognized', 'completed'), ('failed', 'failed'))
    ):
        raise ValueError('OCR request receipt does not match sample status')
    used.add(ident)
    if status == 'failed':
        return None
    frame = row.get('camera_frame')
    if (
        not isinstance(frame, dict)
        or type(frame.get('bytes')) is not int
        or not 32 <= frame['bytes'] <= 768 * 1024
    ):
        raise ValueError('OCR request has no verified frame')
    return frame.get('dimensions')


def score(data, *, diagnostics=None):
    expected = cards()
    if (
        not isinstance(data, dict)
        or set(data) != {'schema', 'corpus', 'samples'}
        or type(data['schema']) is not int
        or data['schema'] != 1
        or data['corpus'] != CORPUS
        or not isinstance(data['samples'], dict)
        or data['samples'].keys() != expected.keys()
    ):
        raise ValueError('Invalid OCR evaluation input')
    samples = []
    receipts = None if diagnostics is None else receipt_index(diagnostics)
    used = set()
    frame_field = 'dimensions' if receipts is None else 'request_id'
    totals = {mode: {'recognized': 0, 'failed': 0, 'not_run': 0} for mode in MODES}
    paired = {mode: {'errors': 0, 'reference_units': 0} for mode in MODES}
    complete_pairs = hd_standard_fallbacks = hd_without_pixel_gain = 0
    for ident, reference in expected.items():
        entry = data['samples'][ident]
        if not isinstance(entry, dict) or set(entry) != set(MODES):
            raise ValueError('Invalid OCR sample modes')
        row = {'id': ident}
        for mode in MODES:
            result = entry[mode]
            if not isinstance(result, dict) or set(result) != {'status', 'text', frame_field}:
                raise ValueError('Invalid OCR sample result')
            status, text = result['status'], result['text']
            dimensions = (
                result[frame_field]
                if receipts is None
                else receipt_dimensions(receipts, used, result[frame_field], status)
            )
            if (
                status not in ('recognized', 'failed', 'not_run')
                or not isinstance(text, str)
                or len(text) > 1000
                or any(unicodedata.category(c) in ('Cc', 'Cs') and c not in '\n\r\t' for c in text)
                or (status != 'recognized' and text)
                or (
                    status == 'recognized'
                    and (not text.strip() or not valid_dimensions(dimensions, mode))
                )
                or (status != 'recognized' and dimensions is not None)
            ):
                raise ValueError('Invalid OCR sample status or text')
            totals[mode][status] += 1
            row[mode] = {'status': status}
            if status == 'recognized':
                marks = text.count(UNREADABLE)
                counts = edit_counts(normalize(reference), normalize(text.replace(UNREADABLE, '')))
                row[mode].update(counts, unreadable_marks=marks, dimensions=dimensions)
        hd_dimensions = row['hd'].get('dimensions')
        fallback = hd_dimensions is not None and valid_dimensions(hd_dimensions, 'standard')
        hd_standard_fallbacks += fallback
        samples.append(row)
        if all(row[mode]['status'] == 'recognized' for mode in MODES) and not fallback:
            if prod(hd_dimensions) <= prod(row['standard']['dimensions']):
                hd_without_pixel_gain += 1
                continue
            complete_pairs += 1
            for mode in MODES:
                for metric in paired[mode]:
                    paired[mode][metric] += row[mode][metric]
    for item in paired.values():
        units = item['reference_units']
        item['cer'] = item['errors'] / units if units else None
    return {
        'schema': 1,
        'corpus': CORPUS,
        'planned_pairs': len(expected),
        'dimension_source': 'request_receipt' if receipts is not None else 'manual',
        'recognized_pairs': complete_pairs,
        'hd_standard_fallbacks': hd_standard_fallbacks,
        'hd_without_pixel_gain': hd_without_pixel_gain,
        'full_coverage': complete_pairs == len(expected),
        'mode_counts': totals,
        'paired_scores': paired,
        'samples': samples,
    }


def valid_dimensions(value, mode):
    if not isinstance(value, list) or len(value) != 2 or any(type(n) is not int for n in value):
        return False
    width, height = MAX_DIMENSIONS[mode]
    return min(value) > 0 and max(value) <= width and prod(value) <= width * height


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--cards', action='store_true', help='Print public test cards as JSON')
    mode.add_argument(
        '--cards-html', action='store_true', help='Print the offline test card sheet as HTML'
    )
    mode.add_argument('--template', action='store_true', help='Write a private input template')
    mode.add_argument('--input', type=Path, help='Score one owner-private JSON input')
    parser.add_argument('--output', type=Path, help='New JSON file in an owner-private directory')
    parser.add_argument(
        '--receipt-template', action='store_true', help='Use request IDs in the template'
    )
    parser.add_argument(
        '--diagnostics', type=Path, help='Private diagnostics export for a receipt template'
    )
    args = parser.parse_args(argv)
    if (args.receipt_template and not args.template) or (args.diagnostics and not args.input):
        parser.error('Use --receipt-template with --template or --diagnostics with --input')
    if args.cards or args.cards_html:
        if args.output:
            parser.error('Public card exports print to stdout')
        if args.cards_html:
            sys.stdout.buffer.write(card_sheet().encode('utf-8'))
        else:
            print(json.dumps(cards(), ensure_ascii=False, indent=2))
        return 0
    if args.output is None:
        parser.error('--output is required for templates and scores')
    try:
        if args.template:
            value = template(use_receipts=args.receipt_template)
        else:
            report = read_private_json(args.diagnostics, 1024 * 1024) if args.diagnostics else None
            if args.diagnostics and report is None:
                raise ValueError('Invalid camera diagnostics report')
            value = score(read_private_json(args.input, 128 * 1024), diagnostics=report)
        with PrivateOutput(args.output) as output:
            output.write(value)
    except (OSError, ValueError, RecursionError):
        print(
            'OCR evaluation failed; check private file access and the fixed template.',
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
