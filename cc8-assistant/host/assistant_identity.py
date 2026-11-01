"""Shared personal assistant name and the existing transcript wake spellings."""

import re

WAKE_WORD = '大黄蜂'
WAKE_PATTERN = re.compile(
    r'大黄蜂|大黃蜂|大黄峰|邦布比|班布比|(?<![A-Za-z])bumble[\s-]*bee(?![A-Za-z])', re.I
)
