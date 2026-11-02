"""Read only fixed QA labels from a bounded in-memory Android hierarchy."""

import re
import xml.etree.ElementTree as ET

from .qa_variant import PACKAGE


def stdout_hierarchy(raw):
    """Accept Android's fixed stdout wrapper, including canonicalized proc PID."""
    if type(raw) is not str or len(raw.encode()) > 256 * 1024:
        raise ValueError('Unsafe UI hierarchy stream')
    # Some versions print the canonical /proc/<pid>/fd/1 instead of /proc/self.
    wrapper = r'UI (?:hierarchy|hierchary) dumped to: /proc/(?:self|[0-9]+)/fd/1'
    raw = re.sub(wrapper, '', raw).strip()
    return hierarchy(raw)


def hierarchy(raw):
    if (
        type(raw) is not str
        or len(raw.encode()) > 256 * 1024
        or '<!DOCTYPE' in raw
        or '<!ENTITY' in raw
    ):
        raise ValueError('Unsafe UI hierarchy')
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise ValueError('Invalid UI hierarchy XML') from None
    if root.tag != 'hierarchy' or sum(1 for _ in root.iter()) > 4096:
        raise ValueError('Invalid UI hierarchy')
    return root


def point(node):
    match = re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', node.get('bounds', ''))
    if not match:
        raise ValueError('Invalid UI bounds')
    left, top, right, bottom = map(int, match.groups())
    if not (0 <= left < right <= 8192 and 0 <= top < bottom <= 8192):
        raise ValueError('UI bounds out of range')
    return (left + right) // 2, (top + bottom) // 2


def status_target(root, message):
    summary = message.split('。', 1)[0] + ' · 查看详情 ›'
    nodes = [
        n for n in root.iter('node') if n.get('package') == PACKAGE and n.get('text') == summary
    ]
    if len(nodes) != 1:
        raise ValueError('Expected one rendered QA error summary')
    parents = {child: parent for parent in root.iter() for child in parent}
    current = nodes[0]
    while current.get('clickable') != 'true':
        current = parents.get(current)
        if current is None:
            raise ValueError('Error details are not clickable')
    if current.get('package') != PACKAGE:
        raise ValueError('QA target left application')
    return point(current)


def dialog_close(root, message):
    messages = [
        n
        for n in root.iter('node')
        if n.get('package') == PACKAGE
        and n.get('resource-id') == 'android:id/message'
        and n.get('text') == message
    ]
    if len(messages) != 1:
        raise ValueError('Full error guidance is not rendered')
    point(messages[0])
    buttons = [
        n
        for n in root.iter('node')
        if n.get('package') == PACKAGE
        and n.get('resource-id') == 'android:id/button1'
        and n.get('text') == '关闭'
        and n.get('clickable') == 'true'
    ]
    if len(buttons) != 1:
        raise ValueError('Expected one QA close button')
    return point(buttons[0])
