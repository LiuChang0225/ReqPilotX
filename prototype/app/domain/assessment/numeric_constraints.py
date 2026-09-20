from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal, Sequence


NUMERIC_REQUIREMENT_MISMATCH_REASON = "numeric_requirement_mismatch"
NUMERIC_CONSTRAINT_UNSUPPORTED_REASON = "numeric_constraint_unsupported"
NUMERIC_EVIDENCE_CONTRADICTION_REASON = "numeric_evidence_contradiction"
NUMERIC_EVIDENCE_UNSAFE_CORRESPONDENCE_REASON = "numeric_evidence_unsafe_correspondence"

_NUMBER = r"(?:\d+(?:\.\d+)?|\.\d+)"
_NUMERIC_TOKEN_PATTERN = re.compile(_NUMBER)
_ENGLISH_NUMBER_WORD_PATTERN = re.compile(
    r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|"
    r"forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion|"
    r"dozens?|tens?|hundreds?|thousands?|millions?|billions?|half|quarter|zeroth|first|second|"
    r"third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"single|double|triple|couple|few|several)\b",
    re.IGNORECASE,
)
_CHINESE_NUMBER_WORD_PATTERN = re.compile(
    r"(?:(?<![\u3400-\u9fff])"
    r"[\u96f6\u3007\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e\u5343\u4e07\u4e24"
    r"\u58f9\u8d30\u53c1\u8086\u4f0d\u9646\u67d2\u634c\u7396\u62fe\u4f70\u4edf\u842c\u534a\u5eff\u5345\u534c]+"
    r"(?![\u3400-\u9fff])|"
    r"[\u96f6\u3007\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e\u5343\u4e07\u4e24"
    r"\u58f9\u8d30\u53c1\u8086\u4f0d\u9646\u67d2\u634c\u7396\u62fe\u4f70\u4edf\u842c\u534a\u5eff\u5345\u534c]+\s*"
    r"(?:\u4f59|\u591a|\u6765)?\s*(?:\u4e2a\s*)?"
    r"(?:\u6beb\u79d2|\u79d2|\u5206\u949f|\u5c0f\u65f6|\u4f0f|\u6beb\u4f0f|"
    r"\u6beb\u7c73|\u7c73|\u5343\u5e15|\u5e15|\u6b27(?:\u59c6)?|\u725b\u987f|"
    r"ms|kpa|pa|v|[\u03a9\u2126]))",
    re.IGNORECASE,
)
_NEGATIVE_WORD_WITH_NUMBER_PATTERN = re.compile(
    rf"(?:\b(?:negative|minus)\b|\u8d1f)\s*{_NUMBER}",
    re.IGNORECASE,
)
_SCOPE_OBLIGATION_PATTERN = re.compile(
    r"(?:\b(?:shall|must|required|requires?|apply|applies|applicable)\b|\u5e94|\u987b|\u9700)",
    re.IGNORECASE,
)
_IDENTIFIER_SAFE_FOLLOWERS = {
    "and",
    "applicable",
    "applies",
    "apply",
    "baseline",
    "channel",
    "cold",
    "compliance",
    "diagnostics",
    "definitions",
    "front",
    "general",
    "has",
    "input",
    "interface",
    "introduction",
    "is",
    "latency",
    "left",
    "mode",
    "must",
    "operation",
    "operating",
    "or",
    "output",
    "overview",
    "passed",
    "pressure",
    "purpose",
    "rear",
    "requirement",
    "requirements",
    "response",
    "right",
    "scope",
    "shall",
    "shutdown",
    "signal",
    "startup",
    "support",
    "supported",
    "supports",
    "test",
    "temperature",
    "time",
    "validation",
    "voltage",
    "wake",
    "warm",
    "was",
}
_CHINESE_IDENTIFIER_SAFE_FOLLOWERS = (
    "\u5e94",
    "\u987b",
    "\u9700",
    "\u9002\u7528",
    "\u652f\u6301",
    "\u9700\u6c42",
    "\u538b\u529b",
    "\u6d4b\u8bd5",
    "\u9a8c\u8bc1",
    "\u5524\u9192",
    "\u542f\u52a8",
    "\u5173\u673a",
    "\u5173\u95ed",
    "\u54cd\u5e94",
    "\u5ef6\u8fdf",
    "\u5de5\u4f5c",
    "\u8f93\u5165",
    "\u8f93\u51fa",
    "\u4f9b\u7535",
)
_LOOSE_NUMBER = r"(?:\d+(?:[.,]\d+)?|[.,]\d+)"
_OPTIONAL_SIGN = r"[+\-\u2212]?"
_UNIT = r"(?:milliseconds?|msecs?|ms|seconds?|secs?|s|volts?|v)"
_RANGE_PATTERN = re.compile(
    rf"(?P<lower>{_NUMBER})\s*(?P<lower_unit>{_UNIT})?\s*"
    rf"(?:[-\u2013\u2014~\uff5e]|\bto\b|\u81f3)\s*"
    rf"(?P<upper>{_NUMBER})\s*(?P<upper_unit>{_UNIT})(?=$|[^\w])",
    re.IGNORECASE,
)
_BETWEEN_RANGE_PATTERN = re.compile(
    rf"(?:\bbetween\b|\u4ecb\u4e8e)\s*(?P<lower>{_NUMBER})\s*"
    rf"(?P<lower_unit>{_UNIT})?\s*(?:\band\b|\u4e0e|\u5230)\s*"
    rf"(?P<upper>{_NUMBER})\s*(?P<upper_unit>{_UNIT})(?=$|[^\w])",
    re.IGNORECASE,
)
_SYMBOL_COMPARISON_PATTERN = re.compile(
    rf"(?P<operator><=|>=|<|>|\u2264|\u2265)\s*"
    rf"(?P<value>{_NUMBER})\s*(?P<unit>{_UNIT})(?=$|[^\w])",
    re.IGNORECASE,
)
_PHRASE_COMPARISON_PATTERN = re.compile(
    rf"(?P<operator>"
    rf"no\s+more\s+than|not\s+greater\s+than|at\s+most|up\s+to|"
    rf"less\s+than|below|under|"
    rf"no\s+less\s+than|not\s+less\s+than|at\s+least|minimum(?:\s+of)?|"
    rf"greater\s+than|above|over|"
    rf"\u4e0d\u8d85\u8fc7|\u81f3\u591a|\u5c0f\u4e8e|\u4f4e\u4e8e|"
    rf"\u4e0d\u5c11\u4e8e|\u81f3\u5c11|\u5927\u4e8e|\u9ad8\u4e8e"
    rf")\s*(?P<value>{_NUMBER})\s*(?P<unit>{_UNIT})(?=$|[^\w])",
    re.IGNORECASE,
)
_VALUE_WITH_UNIT_PATTERN = re.compile(
    rf"(?P<value>{_NUMBER})\s*(?P<unit>{_UNIT})(?=$|[^\w])",
    re.IGNORECASE,
)
_ANY_COMPARISON_WITH_UNIT_PATTERN = re.compile(
    rf"(?:<=|>=|<|>|\u2264|\u2265|less\s+than|greater\s+than|at\s+most|at\s+least|"
    rf"no\s+more\s+than|no\s+less\s+than|not\s+greater\s+than|not\s+less\s+than|"
    rf"up\s+to|minimum(?:\s+of)?|below|under|above|over|"
    rf"\u5c0f\u4e8e|\u5927\u4e8e|\u4f4e\u4e8e|\u9ad8\u4e8e|\u4e0d\u8d85\u8fc7|\u4e0d\u5c11\u4e8e|\u81f3\u591a|\u81f3\u5c11)"
    rf"\s*(?P<value>{_OPTIONAL_SIGN}{_NUMBER})\s*(?P<unit>[A-Za-z\u00b5\u03bc\u00b0%]+)",
    re.IGNORECASE,
)
_ANY_RANGE_PATTERN = re.compile(
    rf"(?P<lower>{_OPTIONAL_SIGN}{_NUMBER})\s*(?P<lower_unit>[A-Za-z\u00b5\u03bc\u00b0%]+)?\s*"
    rf"(?:[-\u2013\u2014~\uff5e]|\bto\b|\u81f3|\.\.)\s*"
    rf"(?P<upper>{_OPTIONAL_SIGN}{_NUMBER})\s*(?P<upper_unit>[A-Za-z\u00b5\u03bc\u00b0%]+)",
    re.IGNORECASE,
)
_TOLERANCE_PATTERN = re.compile(
    rf"{_OPTIONAL_SIGN}{_LOOSE_NUMBER}\s*[\(\[{{\uff08]?\s*(?:\u00b1|\+/-)\s*"
    rf"{_OPTIONAL_SIGN}{_LOOSE_NUMBER}(?:\s*/\s*{_OPTIONAL_SIGN}{_LOOSE_NUMBER})?\s*"
    rf"[\)\]}}\uff09]?\s*(?:[A-Za-z\u00b5\u03bc\u00b0%]+|"
    rf"[\[\(\uff08]\s*[A-Za-z\u00b5\u03bc\u00b0%]+\s*[\]\)\uff09])",
    re.IGNORECASE,
)
_GENERIC_BARE_UNIT = (
    r"(?:milliseconds?|msecs?|ms|seconds?|secs?|s|minutes?|mins?|hours?|hrs?|h|"
    r"microseconds?|nanoseconds?|us|\u00b5s|\u03bcs|ns|"
    r"volts?|v|vdc|vac|mv|kv|pa|kpa|mpa|bar|psi|"
    r"\u00b0?[cfk]|celsius|fahrenheit|kelvin|"
    r"amps?|amperes?|ma|watts?|kw|hz|khz|mhz|%)"
)
_ARTICLE_WITH_MEASUREMENT_PATTERN = re.compile(
    rf"\b(?:a|an)\s+(?:{_GENERIC_BARE_UNIT}|millimet(?:er|re)s?|kilopascals?|pascals?|"
    rf"ohms?|newtons?|joules?|teslas?|webers?|siemens|farads?|henrys?|moles?|"
    rf"candelas?|[\u03a9\u2126])(?=$|[^\w])",
    re.IGNORECASE,
)
_MEASUREMENT_UNIT_LEXEME_PATTERN = re.compile(
    rf"(?:\b(?:milliseconds?|msecs?|seconds?|minutes?|hours?|microseconds?|nanoseconds?|"
    rf"volts?|millivolts?|kilovolts?|amps?|amperes?|watts?|hertz|percent|"
    rf"millimet(?:er|re)s?|met(?:er|re)s?|kilopascals?|pascals?|ohms?|newtons?|"
    rf"dynes?|ergs?|gauss|pounds?[-\s]?force|furlongs?|joules?|teslas?|webers?|"
    rf"siemens|farads?|henrys?|moles?|candelas?)\b|"
    rf"[\u03a9\u2126]|\u6beb\u79d2|\u79d2|\u4f0f|\u6beb\u4f0f|\u5b89\u57f9|\u6beb\u7c73|"
    rf"\u5343\u5e15|\u5e15|\u6b27\u59c6|\u725b\u987f|\u8fbe\u56e0|\u9ad8\u65af)",
    re.IGNORECASE,
)
_FREQUENCY_QUANTITY_PATTERN = re.compile(
    r"(?:\b(?:once|twice|thrice|twofold|threefold|fourfold|fivefold|sixfold|sevenfold|"
    r"eightfold|ninefold|tenfold)\b|"
    r"[\u96f6\u3007\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u4e24"
    r"\u58f9\u8d30\u53c1\u8086\u4f0d\u9646\u67d2\u634c\u7396\u62fe\u51e0]+(?:\u6b21|\u904d))",
    re.IGNORECASE,
)
_ANY_VALUE_WITH_GENERIC_UNIT_PATTERN = re.compile(
    rf"(?<![\w.])(?P<value>{_OPTIONAL_SIGN}{_NUMBER})\s*(?P<unit>{_GENERIC_BARE_UNIT})(?=$|[^\w])",
    re.IGNORECASE,
)
_ATTACHED_SHORT_UNIT_PATTERN = re.compile(
    rf"(?<![\w.])(?P<value>{_OPTIONAL_SIGN}{_NUMBER})(?P<unit>[A-Za-z\u00b5\u03bc\u00b0%]{{1,8}})"
    rf"(?=$|[^\w])",
    re.IGNORECASE,
)
_SEPARATED_SHORT_UNIT_PATTERN = re.compile(
    rf"(?<![\w.])(?P<value>{_OPTIONAL_SIGN}{_NUMBER})(?P<gap>\s+)"
    rf"(?P<unit>[A-Za-z\u00b5\u03bc\u00b0%]{{1,8}})(?=$|[^\w])",
    re.IGNORECASE,
)
_VOLTAGE_QUALIFIER_PATTERN = re.compile(
    rf"(?<![\w.]){_OPTIONAL_SIGN}{_LOOSE_NUMBER}\s*(?:v|volts?)\s*(?:dc|ac|rms|peak|pk)\b",
    re.IGNORECASE,
)
_DETACHED_UNIT_VALUE_PATTERN = re.compile(
    rf"(?:{_OPTIONAL_SIGN}{_LOOSE_NUMBER}\s*[\[\(\uff08]\s*{_GENERIC_BARE_UNIT}\s*"
    rf"[\]\)\uff09]|(?:{_GENERIC_BARE_UNIT}|(?:voltage|time)\s*[\[\(\uff08]\s*"
    rf"{_GENERIC_BARE_UNIT}\s*[\]\)\uff09])\s*[:=]\s*{_OPTIONAL_SIGN}{_LOOSE_NUMBER})",
    re.IGNORECASE,
)
_WORD_NUMBER_WITH_UNIT_PATTERN = _ENGLISH_NUMBER_WORD_PATTERN
_CHINESE_WORD_NUMBER_WITH_UNIT_PATTERN = _CHINESE_NUMBER_WORD_PATTERN
_MEASUREMENT_UNIT_AFTER_IDENTIFIER_PATTERN = re.compile(
    rf"^\s*(?:[-\u2010-\u2015:]\s*)*(?:[\(\[\{{\uff08\u3010]\s*)*"
    rf"(?:{_GENERIC_BARE_UNIT}|millimet(?:er|re)s?|kilopascals?|ohms?|kg|rpm|db|"
    rf"newtons?|newton[-\s]?met(?:er|re)s?|n\s*[\u00b7\u22c5*]\s*m|joules?|"
    rf"teslas?|webers?|siemens|farads?|henrys?|lumens?|lux|moles?|candelas?|"
    rf"[\u03a9\u2126]|\u6beb\u79d2|\u79d2|\u4f0f|\u6beb\u7c73|\u5343\u5e15|\u6b27\u59c6)(?=$|[^\w])",
    re.IGNORECASE,
)
_COMMA_DECIMAL_WITH_UNIT_PATTERN = re.compile(
    rf"(?<![\w.])(?:\d+,\d+|,\d+)\s*{_GENERIC_BARE_UNIT}(?=$|[^\w])",
    re.IGNORECASE,
)
_SCIENTIFIC_WITH_UNIT_PATTERN = re.compile(
    rf"(?<![\w.]){_OPTIONAL_SIGN}{_NUMBER}e{_OPTIONAL_SIGN}\d+\s*{_GENERIC_BARE_UNIT}(?=$|[^\w])",
    re.IGNORECASE,
)
_APPROXIMATE_WITH_UNIT_PATTERN = re.compile(
    rf"(?:~|\u2248|\babout\b|\bapproximately\b|\u7ea6)\s*{_LOOSE_NUMBER}\s*{_GENERIC_BARE_UNIT}(?=$|[^\w])",
    re.IGNORECASE,
)
_SLASH_VALUE_WITH_UNIT_PATTERN = re.compile(
    rf"(?<![\w.]){_LOOSE_NUMBER}\s*/\s*{_LOOSE_NUMBER}\s*{_GENERIC_BARE_UNIT}(?=$|[^\w])",
    re.IGNORECASE,
)
_NONCANONICAL_RANGE_PATTERN = re.compile(
    rf"(?:\[\s*{_LOOSE_NUMBER}\s*,\s*{_LOOSE_NUMBER}\s*\]|"
    rf"{_LOOSE_NUMBER}\s*(?:\u2026|\u22ef)\s*{_LOOSE_NUMBER})\s*{_GENERIC_BARE_UNIT}(?=$|[^\w])",
    re.IGNORECASE,
)
_MULTIPLIED_EXPONENT_WITH_UNIT_PATTERN = re.compile(
    rf"(?<![\w.]){_OPTIONAL_SIGN}{_LOOSE_NUMBER}\s*(?:x|\u00d7|\*)\s*10\s*\^\s*"
    rf"{_OPTIONAL_SIGN}\d+\s*{_GENERIC_BARE_UNIT}(?=$|[^\w])",
    re.IGNORECASE,
)
_GROUPED_NUMBER_WITH_UNIT_PATTERN = re.compile(
    rf"(?<![\w.]){_OPTIONAL_SIGN}\d{{1,3}}(?:[ _\u00a0\u202f\u2009\u2007'\u2019]\d{{3}})+\s*"
    rf"{_GENERIC_BARE_UNIT}(?=$|[^\w])",
    re.IGNORECASE,
)
_NONCANONICAL_UNICODE_NUMERIC_PATTERN = re.compile(
    r"[\uff10-\uff19\uff21-\uff3a\uff41-\uff5a\uff0e\u2212\uff0d\ufe63\u2026\u22ef\u00d7\u00b2\u00b3\u00b9\u2070\u2074-\u2079]"
)
_NON_UNIT_WORDS = {
    "and",
    "at",
    "actual",
    "for",
    "in",
    "is",
    "of",
    "or",
    "passed",
    "requirement",
    "shall",
    "the",
    "to",
    "validated",
    "verified",
    "with",
}
_SUPPORTED_UNIT_TOKENS = {
    "ms",
    "msec",
    "msecs",
    "millisecond",
    "milliseconds",
    "s",
    "sec",
    "secs",
    "second",
    "seconds",
    "v",
    "volt",
    "volts",
}
_SUBJECT_STOP_WORDS = {
    "a",
    "an",
    "and",
    "at",
    "actual",
    "be",
    "component",
    "canonical",
    "device",
    "is",
    "in",
    "latest",
    "measured",
    "measurement",
    "more",
    "must",
    "no",
    "not",
    "of",
    "observed",
    "passed",
    "recorded",
    "result",
    "results",
    "requirement",
    "range",
    "shall",
    "run",
    "runs",
    "support",
    "supported",
    "supports",
    "supplier",
    "system",
    "test",
    "the",
    "time",
    "to",
    "for",
    "up",
    "validation",
    "validated",
    "value",
    "voltage",
    "verified",
}
_CHINESE_SUBJECT_PATTERNS = (
    "\u524d\u90e8",
    "\u540e\u90e8",
    "\u524d\u6392",
    "\u540e\u6392",
    "\u5de6\u4fa7",
    "\u53f3\u4fa7",
    "\u524d\u8f74",
    "\u540e\u8f74",
    "\u524d\u8f6e",
    "\u540e\u8f6e",
    "\u524d",
    "\u540e",
    "\u5de6",
    "\u53f3",
    "\u5524\u9192",
    "\u542f\u52a8",
    "\u5173\u673a",
    "\u5173\u95ed",
    "\u54cd\u5e94",
    "\u5ef6\u8fdf",
    "\u5de5\u4f5c\u7535\u538b",
    "\u8f93\u5165\u7535\u538b",
    "\u4f9b\u7535\u7535\u538b",
)
_IDENTIFIER_WITH_NUMBER_PATTERN = re.compile(
    r"\b(?P<kind>channel|ch|pin|port|zone|mode|stage|sensor|axle|row|column|circuit|ecu|can|bus|rail|id|model)"
    r"\s*(?:no\.?|#|[-_:.])?\s*[\[\(]?\s*(?P<identifier>\d+)\b\s*[\]\)]?",
    re.IGNORECASE,
)
_CHINESE_IDENTIFIER_WITH_NUMBER_PATTERN = re.compile(
    r"(?P<kind>\u901a\u9053|\u5f15\u811a|\u7aef\u53e3|\u533a\u57df|\u6a21\u5f0f|\u9636\u6bb5|\u4f20\u611f\u5668|\u56de\u8def)"
    r"\s*[\(\[\uff08]?\s*(?P<identifier>[0-9\u96f6\u3007\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341"
    r"\u767e\u5343\u4e07\u4e24\u58f9\u8d30\u53c1\u8086\u4f0d\u9646\u67d2\u634c\u7396\u62fe\u4f70\u4edf\u842c]+)"
    r"\s*[\)\]\uff09]?"
)
_CHINESE_REVERSE_IDENTIFIER_PATTERN = re.compile(
    r"\u7b2c?\s*[\(\[\uff08]?\s*(?P<identifier>[0-9\u96f6\u3007\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341"
    r"\u767e\u5343\u4e07\u4e24\u58f9\u8d30\u53c1\u8086\u4f0d\u9646\u67d2\u634c\u7396\u62fe\u4f70\u4edf\u842c]+)"
    r"\s*[\)\]\uff09]?\s*"
    r"(?P<kind>\u901a\u9053|\u5f15\u811a|\u7aef\u53e3|\u533a\u57df|\u6a21\u5f0f|\u9636\u6bb5|\u4f20\u611f\u5668|\u56de\u8def)"
)
_CHINESE_SUFFIX_REFERENCE_SCOPE_PATTERN = re.compile(
    r"(?:\u7b2c\s*)?(?P<identifier>[0-9\u96f6\u3007\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d"
    r"\u5341\u767e\u5343\u4e07\u4e24\u58f9\u8d30\u53c1\u8086\u4f0d\u9646\u67d2\u634c\u7396\u62fe\u4f70\u4edf\u842c"
    r"\u5eff\u5345\u534c]+)\s*(?P<kind>\u8282|\u7ae0|\u6761|\u6b3e|\u8868|\u56fe|\u9875)"
)
_CHINESE_PREFIX_REFERENCE_SCOPE_PATTERN = re.compile(
    r"(?P<kind>\u7248\u672c|\u8868|\u56fe|\u9875)\s*(?:\u7b2c\s*)?"
    r"(?P<identifier>[0-9\u96f6\u3007\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e\u5343\u4e07\u4e24"
    r"\u58f9\u8d30\u53c1\u8086\u4f0d\u9646\u67d2\u634c\u7396\u62fe\u4f70\u4edf\u842c\u5eff\u5345\u534c]+)"
)
_ROMAN_IDENTIFIER_PATTERN = re.compile(
    r"\b(?P<kind>channel|ch|pin|port|zone|mode|stage|sensor|axle|bus|ecu|can|rail)\s*"
    r"(?P<identifier>[ivx\u2160-\u2169]+)\b",
    re.IGNORECASE,
)
_ROMAN_REFERENCE_SCOPE_PATTERN = re.compile(
    r"\b(?P<kind>version|revision|rev|section|sec|clause|chapter|requirement|req|"
    r"table|figure|fig|page)\s*"
    r"(?:no\.?|#|[-_:.])?\s*(?P<identifier>[ivxlcdm\u2160-\u216f]+)\b",
    re.IGNORECASE,
)
_LETTER_IDENTIFIER_PATTERN = re.compile(
    r"\b(?P<kind>channel|ch)\s*(?:no\.?|#|[-_:.])?\s*[\[\(]?\s*"
    r"(?P<identifier>[a-z])\b\s*[\]\)]?",
    re.IGNORECASE,
)
_WORD_IDENTIFIER_PATTERN = re.compile(
    r"\b(?P<kind>channel|ch|pin|port|zone|mode|stage|sensor|axle|row|column|circuit|"
    r"ecu|can|bus|rail)\s*(?:no\.?|#|[-_:.])?\s*"
    r"(?P<identifier>zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
    r"zeroth|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh|"
    r"twelfth|thirteenth|fourteenth|fifteenth|sixteenth|seventeenth|eighteenth|"
    r"nineteenth|twentieth)\b",
    re.IGNORECASE,
)
_REVERSE_WORD_IDENTIFIER_PATTERN = re.compile(
    r"\b(?:the\s+)?(?P<identifier>zeroth|first|second|third|fourth|fifth|sixth|seventh|"
    r"eighth|ninth|tenth|eleventh|twelfth|thirteenth|fourteenth|fifteenth|sixteenth|"
    r"seventeenth|eighteenth|nineteenth|twentieth)\s+"
    r"(?P<kind>channel|pin|port|zone|mode|stage|sensor|axle|row|column|circuit|ecu|can|bus|rail)\b",
    re.IGNORECASE,
)
_IDENTIFIER_PREFIX_PATTERN = re.compile(
    r"(?:\bchannel|\bch|\bpin|\bport|\bzone|\bmode|\bstage|\bsensor|\baxle|\brow|"
    r"\bcolumn|\bcircuit|\becu|\bcan|\bbus|\brail|\bsection|\bclause|\bchapter|\brequirement|\breq|"
    r"\biso|\bversion)\s*(?:no\.?|#|[-_:.])?\s*\[?\s*$",
    re.IGNORECASE,
)
_REFERENCE_IDENTIFIER_PATTERN = re.compile(
    r"\b(?:section|sec|clause|chapter|requirement|req|iso|version|revision|rev|table|figure|fig|page)"
    r"\s*(?:no\.?|#|[-_:.])?\s*\d+(?:\.\d+)*\b",
    re.IGNORECASE,
)
_REFERENCE_SCOPE_PATTERN = re.compile(
    r"\b(?P<kind>iso|version|revision|rev|section|sec|clause|chapter|requirement|req|"
    r"table|figure|fig|page|year|my|model\s+year)"
    r"\s*(?:no\.?|#|[-_:.])?\s*(?P<identifier>\d+(?:\.\d+)*)\b",
    re.IGNORECASE,
)
_LETTER_REFERENCE_SCOPE_PATTERN = re.compile(
    r"\b(?P<kind>version|revision|rev|section|sec|clause|chapter|requirement|req|"
    r"table|figure|fig|page)\s*(?:no\.?|#|[-_:.])?\s*"
    r"(?P<identifier>[abefghjknopqrstuwyz])\b",
    re.IGNORECASE,
)
_WORD_REFERENCE_SCOPE_PATTERN = re.compile(
    r"\b(?P<kind>version|revision|rev|section|sec|clause|chapter|requirement|req|"
    r"table|figure|fig|page)\s*(?:no\.?|#|[-_:.])?\s*"
    r"(?P<identifier>zeroth|first|second|third|fourth|fifth|sixth|seventh|eighth|"
    r"ninth|tenth|eleventh|twelfth|thirteenth|fourteenth|fifteenth|sixteenth|"
    r"seventeenth|eighteenth|nineteenth|twentieth)\b",
    re.IGNORECASE,
)
_ISO_EXTENDED_SCOPE_PATTERN = re.compile(
    r"\biso\s*(?P<identifier>\d+(?:(?P<separator>[:-])\d+)?)\b",
    re.IGNORECASE,
)
_SHORT_VERSION_SCOPE_PATTERN = re.compile(r"\bv\s*(?P<identifier>\d+)\b", re.IGNORECASE)
_LEADING_SECTION_PATTERN = re.compile(r"(?m)^\s*\d+(?:\.\d+)+(?=\s|$)")
_YEAR_IDENTIFIER_PATTERN = re.compile(
    r"(?:\b(?:year|model\s+year|my)\s*(?:19|20)\d{2}\b|\b(?:19|20)\d{2}\s+baseline\b)",
    re.IGNORECASE,
)
_DATE_IDENTIFIER_PATTERN = re.compile(
    r"\b(?:19|20)\d{2}[-/.](?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12]\d|3[01])\b"
)


@dataclass(frozen=True)
class NumericConstraint:
    dimension: Literal["time", "voltage"]
    lower: Decimal | None
    upper: Decimal | None
    lower_inclusive: bool
    upper_inclusive: bool
    subject_tokens: tuple[str, ...] = ()
    negated: bool = False

    @property
    def is_range(self) -> bool:
        return self.lower is not None and self.upper is not None and self.lower != self.upper


def validate_numeric_requirement_projection(
    *,
    raw_requirement: str,
    normalized_requirement: str,
    candidate_requirement: str,
) -> str:
    """Reject model-authored numeric constraints that differ from server-owned input."""

    if any(
        _has_unsupported_numeric_contract(text)
        for text in (raw_requirement, normalized_requirement, candidate_requirement)
    ):
        return NUMERIC_CONSTRAINT_UNSUPPORTED_REASON

    server_constraints = _server_requirement_constraints(
        raw_requirement=raw_requirement,
        normalized_requirement=normalized_requirement,
    )
    candidate_constraints = extract_numeric_constraints(candidate_requirement)
    server_scopes = _standalone_scope_tokens(raw_requirement) | _standalone_scope_tokens(
        normalized_requirement
    )
    candidate_scopes = _standalone_scope_tokens(candidate_requirement)
    scopes_match = bool(server_constraints) or server_scopes == candidate_scopes
    if scopes_match and _constraint_set(server_constraints) == _constraint_set(candidate_constraints):
        return ""
    return NUMERIC_REQUIREMENT_MISMATCH_REASON


def validate_numeric_evidence_support(
    *,
    raw_requirement: str,
    normalized_requirement: str,
    evidence_texts: Sequence[str],
) -> str:
    """Require canonical evidence to safely entail each supported numeric constraint."""

    return validate_numeric_decision_support(
        decision="ACCEPT",
        raw_requirement=raw_requirement,
        normalized_requirement=normalized_requirement,
        evidence_texts=evidence_texts,
    )


def validate_numeric_decision_support(
    *,
    decision: Literal["ACCEPT", "REJECT", "DEVIATION_ACCEPT"],
    raw_requirement: str,
    normalized_requirement: str,
    evidence_texts: Sequence[str],
) -> str:
    """Validate numeric correspondence for every deterministic serving decision."""

    if _has_unsupported_numeric_contract(raw_requirement) or _has_unsupported_numeric_contract(
        normalized_requirement
    ):
        return NUMERIC_CONSTRAINT_UNSUPPORTED_REASON

    requirements = _server_requirement_constraints(
        raw_requirement=raw_requirement,
        normalized_requirement=normalized_requirement,
    )
    if not requirements:
        required_scopes = _standalone_scope_tokens(raw_requirement) | _standalone_scope_tokens(
            normalized_requirement
        )
        if required_scopes and not _scope_sets_are_compatible(
            required_scopes,
            tuple(_standalone_scope_tokens(text) for text in evidence_texts),
        ):
            return NUMERIC_EVIDENCE_UNSAFE_CORRESPONDENCE_REASON
        return ""

    evidence_constraints: list[NumericConstraint] = []
    for text in evidence_texts:
        if _has_unsupported_numeric_contract(text):
            return NUMERIC_CONSTRAINT_UNSUPPORTED_REASON
        evidence_constraints.extend(extract_numeric_constraints(text))
    evidence_by_dimension = _group_by_dimension(tuple(evidence_constraints))

    saw_rejecting_contradiction = False
    for requirement in requirements:
        candidates = tuple(
            evidence
            for evidence in evidence_by_dimension.get(requirement.dimension, ())
            if _subjects_are_compatible(requirement, evidence)
        )
        if not candidates:
            if decision != "REJECT":
                return NUMERIC_EVIDENCE_UNSAFE_CORRESPONDENCE_REASON
            continue

        relations = tuple(_constraint_relation(requirement, evidence) for evidence in candidates)
        if decision == "REJECT":
            if "contradicts" in relations and "supports" in relations:
                return NUMERIC_EVIDENCE_UNSAFE_CORRESPONDENCE_REASON
            if "contradicts" in relations:
                saw_rejecting_contradiction = True
            continue
        if "contradicts" in relations:
            return NUMERIC_EVIDENCE_CONTRADICTION_REASON
        if requirement.is_range:
            if not any(_same_constraint(requirement, evidence) for evidence in candidates):
                return NUMERIC_EVIDENCE_UNSAFE_CORRESPONDENCE_REASON
            continue
        if "supports" not in relations:
            return NUMERIC_EVIDENCE_UNSAFE_CORRESPONDENCE_REASON
    if decision == "REJECT" and not saw_rejecting_contradiction:
        return NUMERIC_EVIDENCE_UNSAFE_CORRESPONDENCE_REASON
    return ""


def extract_numeric_constraints(text: str) -> tuple[NumericConstraint, ...]:
    """Extract the intentionally small supported surface: time and voltage bounds/ranges."""

    constraints, _ = _extract_numeric_constraints_with_spans(text)
    return constraints


def _extract_numeric_constraints_with_spans(
    text: str,
) -> tuple[tuple[NumericConstraint, ...], tuple[tuple[int, int], ...]]:

    value = str(text or "")
    constraints: list[NumericConstraint] = []
    occupied: list[tuple[int, int]] = []

    for pattern in (_BETWEEN_RANGE_PATTERN, _RANGE_PATTERN):
        for match in pattern.finditer(value):
            if _overlaps(match.span(), occupied):
                continue
            lower_unit = str(match.group("lower_unit") or match.group("upper_unit") or "")
            upper_unit = str(match.group("upper_unit") or lower_unit)
            lower = _normalized_value(match.group("lower"), lower_unit)
            upper = _normalized_value(match.group("upper"), upper_unit)
            if lower is None or upper is None or lower[0] != upper[0]:
                continue
            constraints.append(
                NumericConstraint(
                    dimension=lower[0],
                    lower=lower[1],
                    upper=upper[1],
                    lower_inclusive=True,
                    upper_inclusive=True,
                    subject_tokens=_subject_tokens(value, match.span()),
                    negated=_is_negated(value, match.start()),
                )
            )
            occupied.append(match.span())

    for pattern in (_SYMBOL_COMPARISON_PATTERN, _PHRASE_COMPARISON_PATTERN):
        for match in pattern.finditer(value):
            if _overlaps(match.span(), occupied):
                continue
            normalized = _normalized_value(match.group("value"), match.group("unit"))
            if normalized is None:
                continue
            operator = _normalize_operator(match.group("operator"))
            dimension, number = normalized
            if operator in {"<", "<="}:
                constraint = NumericConstraint(
                    dimension=dimension,
                    lower=None,
                    upper=number,
                    lower_inclusive=False,
                    upper_inclusive=operator == "<=",
                    subject_tokens=_subject_tokens(value, match.span()),
                    negated=_is_negated(value, match.start()),
                )
            else:
                constraint = NumericConstraint(
                    dimension=dimension,
                    lower=number,
                    upper=None,
                    lower_inclusive=operator == ">=",
                    upper_inclusive=False,
                    subject_tokens=_subject_tokens(value, match.span()),
                    negated=_is_negated(value, match.start()),
                )
            constraints.append(constraint)
            occupied.append(match.span())

    for match in _VALUE_WITH_UNIT_PATTERN.finditer(value):
        if _overlaps(match.span(), occupied):
            continue
        normalized = _normalized_value(match.group("value"), match.group("unit"))
        if normalized is None:
            continue
        dimension, number = normalized
        constraints.append(
            NumericConstraint(
                dimension=dimension,
                lower=number,
                upper=number,
                lower_inclusive=True,
                upper_inclusive=True,
                subject_tokens=_subject_tokens(value, match.span()),
                negated=_is_negated(value, match.start()),
            )
        )
        occupied.append(match.span())

    unique: list[NumericConstraint] = []
    seen: set[NumericConstraint] = set()
    for constraint in constraints:
        if constraint in seen:
            continue
        unique.append(constraint)
        seen.add(constraint)
    return tuple(unique), tuple(occupied)


def _server_requirement_constraints(
    *,
    raw_requirement: str,
    normalized_requirement: str,
) -> tuple[NumericConstraint, ...]:
    return _dedupe_constraints(
        (*extract_numeric_constraints(raw_requirement), *extract_numeric_constraints(normalized_requirement))
    )


def _normalized_value(
    raw_value: str,
    raw_unit: str,
) -> tuple[Literal["time", "voltage"], Decimal] | None:
    try:
        value = Decimal(str(raw_value))
    except InvalidOperation:
        return None
    unit = str(raw_unit or "").strip().lower()
    if unit in {"ms", "msec", "msecs", "millisecond", "milliseconds"}:
        return "time", value
    if unit in {"s", "sec", "secs", "second", "seconds"}:
        return "time", value * Decimal("1000")
    if unit in {"v", "volt", "volts"}:
        return "voltage", value
    return None


def _normalize_operator(value: str) -> Literal["<", "<=", ">", ">="]:
    operator = re.sub(r"\s+", " ", str(value or "").strip().lower())
    if operator in {"<", "\u5c0f\u4e8e", "\u4f4e\u4e8e", "less than", "below", "under"}:
        return "<"
    if operator in {
        "<=",
        "\u2264",
        "\u4e0d\u8d85\u8fc7",
        "\u81f3\u591a",
        "no more than",
        "not greater than",
        "at most",
        "up to",
    }:
        return "<="
    if operator in {">", "\u5927\u4e8e", "\u9ad8\u4e8e", "greater than", "above", "over"}:
        return ">"
    return ">="


def _group_by_dimension(
    constraints: tuple[NumericConstraint, ...],
) -> dict[str, tuple[NumericConstraint, ...]]:
    grouped: dict[str, list[NumericConstraint]] = {}
    for constraint in constraints:
        grouped.setdefault(constraint.dimension, []).append(constraint)
    return {dimension: tuple(items) for dimension, items in grouped.items()}


def _constraint_relation(
    requirement: NumericConstraint,
    evidence: NumericConstraint,
) -> Literal["supports", "contradicts", "unsafe"]:
    if requirement.dimension != evidence.dimension or not _subjects_are_compatible(requirement, evidence):
        return "unsafe"
    if requirement.negated or evidence.negated:
        return "unsafe"
    if _invalid_interval(requirement) or _invalid_interval(evidence):
        return "unsafe"
    if _is_subset(evidence, requirement):
        return "supports"
    if _intervals_are_disjoint(requirement, evidence):
        return "contradicts"
    return "unsafe"


def _is_subset(candidate: NumericConstraint, container: NumericConstraint) -> bool:
    return _lower_is_within(candidate, container) and _upper_is_within(candidate, container)


def _lower_is_within(candidate: NumericConstraint, container: NumericConstraint) -> bool:
    if container.lower is None:
        return True
    if candidate.lower is None:
        return False
    if candidate.lower > container.lower:
        return True
    if candidate.lower < container.lower:
        return False
    return container.lower_inclusive or not candidate.lower_inclusive


def _upper_is_within(candidate: NumericConstraint, container: NumericConstraint) -> bool:
    if container.upper is None:
        return True
    if candidate.upper is None:
        return False
    if candidate.upper < container.upper:
        return True
    if candidate.upper > container.upper:
        return False
    return container.upper_inclusive or not candidate.upper_inclusive


def _intervals_are_disjoint(left: NumericConstraint, right: NumericConstraint) -> bool:
    if left.upper is not None and right.lower is not None:
        if left.upper < right.lower:
            return True
        if left.upper == right.lower and not (left.upper_inclusive and right.lower_inclusive):
            return True
    if right.upper is not None and left.lower is not None:
        if right.upper < left.lower:
            return True
        if right.upper == left.lower and not (right.upper_inclusive and left.lower_inclusive):
            return True
    return False


def _same_constraint(left: NumericConstraint, right: NumericConstraint) -> bool:
    return (
        not left.negated
        and not right.negated
        and _numeric_signature(left) == _numeric_signature(right)
        and _subjects_are_compatible(left, right)
    )


def _invalid_interval(constraint: NumericConstraint) -> bool:
    if constraint.lower is None or constraint.upper is None:
        return False
    if constraint.lower > constraint.upper:
        return True
    if constraint.lower == constraint.upper:
        return not (constraint.lower_inclusive and constraint.upper_inclusive)
    return False


def _constraint_set(constraints: tuple[NumericConstraint, ...]) -> frozenset[tuple[object, ...]]:
    return frozenset(
        (*_numeric_signature(constraint), constraint.subject_tokens, constraint.negated)
        for constraint in constraints
    )


def _numeric_signature(constraint: NumericConstraint) -> tuple[object, ...]:
    return (
        constraint.dimension,
        constraint.lower,
        constraint.upper,
        constraint.lower_inclusive,
        constraint.upper_inclusive,
    )


def _dedupe_constraints(constraints: Sequence[NumericConstraint]) -> tuple[NumericConstraint, ...]:
    unique: list[NumericConstraint] = []
    for constraint in constraints:
        if any(
            _numeric_signature(constraint) == _numeric_signature(existing)
            and constraint.negated == existing.negated
            and _subjects_are_compatible(constraint, existing)
            for existing in unique
        ):
            continue
        unique.append(constraint)
    return tuple(unique)


def _subjects_are_compatible(left: NumericConstraint, right: NumericConstraint) -> bool:
    return left.subject_tokens == right.subject_tokens


def _subject_tokens(text: str, span: tuple[int, int]) -> tuple[str, ...]:
    start, end = span
    clause_start = max(
        text.rfind(";", 0, start),
        text.rfind("\n", 0, start),
        text.rfind("\u3002", 0, start),
        text.rfind("\uff1b", 0, start),
    ) + 1
    clause_ends = [
        position
        for delimiter in (";", "\n", "\u3002", "\uff1b")
        if (position := text.find(delimiter, end)) >= 0
    ]
    clause_end = min(clause_ends) if clause_ends else len(text)
    clause = text[clause_start:clause_end]
    relative_start = start - clause_start
    relative_end = end - clause_start
    sentence_start, sentence_end = _sentence_bounds(clause, relative_start, relative_end)
    clause = clause[sentence_start:sentence_end]
    relative_start -= sentence_start
    relative_end -= sentence_start
    subject_prefix = clause[:relative_start]
    subject_suffix = clause[relative_end:]
    subject_separator = (
        r"[,\uff0c]|[:\uff1a](?!\d)|\b(?:and|but|while|whereas)\b|"
        r"(?:\u4ee5\u53ca|\u5e76\u4e14|\u800c|\u3001)"
    )
    prefix_segments = re.split(
        subject_separator,
        subject_prefix,
        flags=re.IGNORECASE,
    )
    suffix_segments = re.split(
        subject_separator,
        subject_suffix,
        flags=re.IGNORECASE,
    )
    subject_source = f"{prefix_segments[-1]} {suffix_segments[0]}"

    tokens = {
        token
        for token in re.findall(r"[a-z]+", subject_source.lower())
        if token not in _SUBJECT_STOP_WORDS and token not in _SUPPORTED_UNIT_TOKENS
    }
    for marker in _CHINESE_SUBJECT_PATTERNS:
        if marker in subject_source:
            tokens.add(marker)
    for match in _IDENTIFIER_WITH_NUMBER_PATTERN.finditer(subject_source):
        kind = _normalize_identifier_kind(match.group("kind"))
        tokens.discard(match.group("kind").lower())
        tokens.add(
            f"{kind}:{_normalize_identifier(match.group('identifier'))}"
        )
    for match in _CHINESE_IDENTIFIER_WITH_NUMBER_PATTERN.finditer(subject_source):
        tokens.add(f"{match.group('kind')}:{_normalize_identifier(match.group('identifier'))}")
    for match in _CHINESE_REVERSE_IDENTIFIER_PATTERN.finditer(subject_source):
        tokens.add(f"{match.group('kind')}:{_normalize_identifier(match.group('identifier'))}")
    for pattern in (
        _CHINESE_SUFFIX_REFERENCE_SCOPE_PATTERN,
        _CHINESE_PREFIX_REFERENCE_SCOPE_PATTERN,
    ):
        for match in pattern.finditer(subject_source):
            tokens.add(
                f"{_normalize_chinese_reference_kind(match.group('kind'))}:"
                f"{_normalize_identifier(match.group('identifier'))}"
            )
    for match in _ROMAN_IDENTIFIER_PATTERN.finditer(subject_source):
        tokens.discard(match.group("identifier").lower())
        tokens.discard(match.group("kind").lower())
        tokens.add(
            f"{_normalize_identifier_kind(match.group('kind'))}:"
            f"{_normalize_identifier(match.group('identifier'))}"
        )
    for match in _WORD_IDENTIFIER_PATTERN.finditer(subject_source):
        tokens.discard(match.group("identifier").lower())
        tokens.discard(match.group("kind").lower())
        tokens.add(
            f"{_normalize_identifier_kind(match.group('kind'))}:"
            f"{_normalize_identifier(match.group('identifier'))}"
        )
    for match in _REVERSE_WORD_IDENTIFIER_PATTERN.finditer(subject_source):
        tokens.discard(match.group("identifier").lower())
        tokens.discard(match.group("kind").lower())
        tokens.add(
            f"{_normalize_identifier_kind(match.group('kind'))}:"
            f"{_normalize_identifier(match.group('identifier'))}"
        )
    for match in _LETTER_IDENTIFIER_PATTERN.finditer(subject_source):
        tokens.discard(match.group("identifier").lower())
        tokens.discard(match.group("kind").lower())
        tokens.add(
            f"{_normalize_identifier_kind(match.group('kind'))}:"
            f"{_normalize_identifier(match.group('identifier'))}"
        )
    reference_kind_aliases = {
        "rev": "revision",
        "sec": "section",
        "req": "requirement",
        "fig": "figure",
        "my": "year",
        "model year": "year",
    }
    for match in _ROMAN_REFERENCE_SCOPE_PATTERN.finditer(subject_source):
        raw_kind = match.group("kind").lower()
        tokens.discard(raw_kind)
        tokens.discard(match.group("identifier").lower())
        kind = reference_kind_aliases.get(raw_kind, raw_kind)
        tokens.add(f"{kind}:{_normalize_identifier(match.group('identifier'))}")
    for match in _LETTER_REFERENCE_SCOPE_PATTERN.finditer(subject_source):
        raw_kind = match.group("kind").lower()
        tokens.discard(raw_kind)
        tokens.discard(match.group("identifier").lower())
        kind = reference_kind_aliases.get(raw_kind, raw_kind)
        tokens.add(f"{kind}:{match.group('identifier').casefold()}")
    for match in _WORD_REFERENCE_SCOPE_PATTERN.finditer(subject_source):
        raw_kind = match.group("kind").lower()
        tokens.discard(raw_kind)
        tokens.discard(match.group("identifier").lower())
        kind = reference_kind_aliases.get(raw_kind, raw_kind)
        tokens.add(f"{kind}:{_normalize_identifier(match.group('identifier'))}")
    for match in _REFERENCE_SCOPE_PATTERN.finditer(subject_source):
        raw_kind = re.sub(r"\s+", " ", match.group("kind").lower())
        for word in raw_kind.split():
            tokens.discard(word)
        kind = reference_kind_aliases.get(raw_kind, raw_kind)
        tokens.add(f"{kind}:{_normalize_identifier(match.group('identifier'))}")
    for match in _ISO_EXTENDED_SCOPE_PATTERN.finditer(subject_source):
        separator = match.group("separator") or ""
        identifier = match.group("identifier").replace(separator, ":") if separator else match.group("identifier")
        tokens.add(f"iso:{identifier}")
    for match in _SHORT_VERSION_SCOPE_PATTERN.finditer(subject_source):
        tokens.discard("v")
        tokens.add(f"version:{_normalize_identifier(match.group('identifier'))}")
    for match in _YEAR_IDENTIFIER_PATTERN.finditer(subject_source):
        year_match = re.search(r"(?:19|20)\d{2}", match.group(0))
        if year_match:
            tokens.discard("baseline")
            tokens.discard("model")
            tokens.discard("my")
            tokens.discard("year")
            tokens.add(f"year:{year_match.group(0)}")
    for match in _DATE_IDENTIFIER_PATTERN.finditer(subject_source):
        tokens.add(f"date:{_normalize_date_identifier(match.group(0))}")
    date_spans = tuple(match.span() for match in _DATE_IDENTIFIER_PATTERN.finditer(subject_source))
    for match in _LEADING_SECTION_PATTERN.finditer(subject_source):
        if _overlaps(match.span(), date_spans):
            continue
        section_match = re.search(r"\d+(?:\.\d+)+", match.group(0))
        if section_match:
            tokens.add(f"section:{section_match.group(0)}")
    return tuple(sorted(tokens))


def _sentence_bounds(text: str, span_start: int, span_end: int) -> tuple[int, int]:
    boundaries: list[int] = []
    abbreviations = {"ch", "fig", "max", "min", "no", "rev", "sec"}
    for index, character in enumerate(text):
        if character not in {".", "!", "?", "\uff01", "\uff1f"}:
            continue
        if character == ".":
            previous = text[index - 1] if index > 0 else ""
            following = text[index + 1] if index + 1 < len(text) else ""
            if previous.isdigit() and following.isdigit():
                continue
            word_match = re.search(r"([A-Za-z]+)$", text[:index])
            following_text = text[index + 1 :].lstrip()
            following_head = following_text[:1]
            if word_match and word_match.group(1).lower() in abbreviations:
                abbreviation = word_match.group(1).lower()
                if abbreviation == "ch" and following_head and following_head.isalnum():
                    continue
                current_sentence_start = boundaries[-1] + 1 if boundaries else 0
                current_segment = text[current_sentence_start:index]
                has_measurement_before_abbreviation = any(
                    pattern.search(current_segment)
                    for pattern in (
                        _VALUE_WITH_UNIT_PATTERN,
                        _ANY_VALUE_WITH_GENERIC_UNIT_PATTERN,
                        _ENGLISH_NUMBER_WORD_PATTERN,
                        _CHINESE_NUMBER_WORD_PATTERN,
                    )
                )
                if following_head and not has_measurement_before_abbreviation:
                    continue
        boundaries.append(index)
    sentence_start = max((index + 1 for index in boundaries if index < span_start), default=0)
    sentence_end = min((index for index in boundaries if index >= span_end), default=len(text))
    return sentence_start, sentence_end


def _normalize_identifier(value: str) -> str:
    identifier = str(value or "").strip()
    if identifier.isdigit():
        return str(int(identifier))
    english_numbers = {
        word: str(index)
        for index, word in enumerate(
            (
                "zero",
                "one",
                "two",
                "three",
                "four",
                "five",
                "six",
                "seven",
                "eight",
                "nine",
                "ten",
                "eleven",
                "twelve",
                "thirteen",
                "fourteen",
                "fifteen",
                "sixteen",
                "seventeen",
                "eighteen",
                "nineteen",
                "twenty",
            )
        )
    }
    english_numbers.update(
        {
            word: str(index)
            for index, word in enumerate(
                (
                    "zeroth",
                    "first",
                    "second",
                    "third",
                    "fourth",
                    "fifth",
                    "sixth",
                    "seventh",
                    "eighth",
                    "ninth",
                    "tenth",
                    "eleventh",
                    "twelfth",
                    "thirteenth",
                    "fourteenth",
                    "fifteenth",
                    "sixteenth",
                    "seventeenth",
                    "eighteenth",
                    "nineteenth",
                    "twentieth",
                )
            )
        }
    )
    roman_numbers = {
        "i": "1",
        "ii": "2",
        "iii": "3",
        "iv": "4",
        "v": "5",
        "vi": "6",
        "vii": "7",
        "viii": "8",
        "ix": "9",
        "x": "10",
        "\u2160": "1",
        "\u2161": "2",
        "\u2162": "3",
        "\u2163": "4",
        "\u2164": "5",
        "\u2165": "6",
        "\u2166": "7",
        "\u2167": "8",
        "\u2168": "9",
        "\u2169": "10",
    }
    chinese_number = _parse_chinese_integer(identifier)
    if chinese_number is not None:
        return str(chinese_number)
    return english_numbers.get(
        identifier.casefold(),
        roman_numbers.get(identifier, roman_numbers.get(identifier.casefold(), identifier)),
    )


def _normalize_identifier_kind(value: str) -> str:
    kind = str(value or "").strip().lower()
    return {"ch": "channel"}.get(kind, kind)


def _normalize_chinese_reference_kind(value: str) -> str:
    return {
        "\u8282": "section",
        "\u7ae0": "chapter",
        "\u6761": "clause",
        "\u6b3e": "clause",
        "\u8868": "table",
        "\u56fe": "figure",
        "\u9875": "page",
        "\u7248\u672c": "version",
    }.get(str(value or "").strip(), str(value or "").strip())


def _parse_chinese_integer(value: str) -> int | None:
    shorthand = {"\u5eff": 20, "\u5345": 30, "\u534c": 40}
    if value in shorthand:
        return shorthand[value]
    digits = {
        "\u96f6": 0,
        "\u3007": 0,
        "\u4e00": 1,
        "\u58f9": 1,
        "\u4e8c": 2,
        "\u4e24": 2,
        "\u8d30": 2,
        "\u4e09": 3,
        "\u53c1": 3,
        "\u56db": 4,
        "\u8086": 4,
        "\u4e94": 5,
        "\u4f0d": 5,
        "\u516d": 6,
        "\u9646": 6,
        "\u4e03": 7,
        "\u67d2": 7,
        "\u516b": 8,
        "\u634c": 8,
        "\u4e5d": 9,
        "\u7396": 9,
    }
    units = {"\u5341": 10, "\u62fe": 10, "\u767e": 100, "\u4f70": 100, "\u5343": 1000, "\u4edf": 1000}
    if not value or any(character not in digits and character not in units for character in value):
        return None
    if all(character in digits for character in value):
        return int("".join(str(digits[character]) for character in value))
    total = 0
    current = 0
    for character in value:
        if character in digits:
            current = digits[character]
            continue
        unit = units[character]
        total += (current or 1) * unit
        current = 0
    return total + current


def _normalize_date_identifier(value: str) -> str:
    year, month, day = (int(part) for part in re.split(r"[-/.]", value))
    return f"{year:04d}-{month:02d}-{day:02d}"


def _standalone_scope_tokens(text: str) -> frozenset[str]:
    value = str(text or "")
    tokens: set[str] = set()
    identifier_patterns = (
        _IDENTIFIER_WITH_NUMBER_PATTERN,
        _ROMAN_IDENTIFIER_PATTERN,
        _WORD_IDENTIFIER_PATTERN,
        _REVERSE_WORD_IDENTIFIER_PATTERN,
        _LETTER_IDENTIFIER_PATTERN,
    )
    for pattern in identifier_patterns:
        for match in pattern.finditer(value):
            tokens.add(
                f"{_normalize_identifier_kind(match.group('kind'))}:"
                f"{_normalize_identifier(match.group('identifier'))}"
            )
    for pattern in (_CHINESE_IDENTIFIER_WITH_NUMBER_PATTERN, _CHINESE_REVERSE_IDENTIFIER_PATTERN):
        for match in pattern.finditer(value):
            tokens.add(f"{match.group('kind')}:{_normalize_identifier(match.group('identifier'))}")
    for pattern in (
        _CHINESE_SUFFIX_REFERENCE_SCOPE_PATTERN,
        _CHINESE_PREFIX_REFERENCE_SCOPE_PATTERN,
    ):
        for match in pattern.finditer(value):
            tokens.add(
                f"{_normalize_chinese_reference_kind(match.group('kind'))}:"
                f"{_normalize_identifier(match.group('identifier'))}"
            )
    aliases = {
        "rev": "revision",
        "sec": "section",
        "req": "requirement",
        "fig": "figure",
        "my": "year",
        "model year": "year",
    }
    for match in _ROMAN_REFERENCE_SCOPE_PATTERN.finditer(value):
        raw_kind = match.group("kind").lower()
        tokens.add(
            f"{aliases.get(raw_kind, raw_kind)}:{_normalize_identifier(match.group('identifier'))}"
        )
    for match in _LETTER_REFERENCE_SCOPE_PATTERN.finditer(value):
        raw_kind = match.group("kind").lower()
        tokens.add(f"{aliases.get(raw_kind, raw_kind)}:{match.group('identifier').casefold()}")
    for match in _WORD_REFERENCE_SCOPE_PATTERN.finditer(value):
        raw_kind = match.group("kind").lower()
        tokens.add(
            f"{aliases.get(raw_kind, raw_kind)}:{_normalize_identifier(match.group('identifier'))}"
        )
    for match in _REFERENCE_SCOPE_PATTERN.finditer(value):
        raw_kind = re.sub(r"\s+", " ", match.group("kind").lower())
        tokens.add(
            f"{aliases.get(raw_kind, raw_kind)}:{_normalize_identifier(match.group('identifier'))}"
        )
    for match in _ISO_EXTENDED_SCOPE_PATTERN.finditer(value):
        separator = match.group("separator") or ""
        identifier = match.group("identifier").replace(separator, ":") if separator else match.group("identifier")
        tokens.add(f"iso:{identifier}")
    for match in _SHORT_VERSION_SCOPE_PATTERN.finditer(value):
        tokens.add(f"version:{_normalize_identifier(match.group('identifier'))}")
    for match in _YEAR_IDENTIFIER_PATTERN.finditer(value):
        year = re.search(r"(?:19|20)\d{2}", match.group(0))
        if year:
            tokens.add(f"year:{year.group(0)}")
    for match in _DATE_IDENTIFIER_PATTERN.finditer(value):
        tokens.add(f"date:{_normalize_date_identifier(match.group(0))}")
    date_spans = tuple(match.span() for match in _DATE_IDENTIFIER_PATTERN.finditer(value))
    if _SCOPE_OBLIGATION_PATTERN.search(value):
        for match in _LEADING_SECTION_PATTERN.finditer(value):
            if _overlaps(match.span(), date_spans):
                continue
            section = re.search(r"\d+(?:\.\d+)+", match.group(0))
            if section:
                tokens.add(f"section:{section.group(0)}")
    return frozenset(tokens)


def _scope_sets_are_compatible(
    required: frozenset[str],
    evidence_sets: Sequence[frozenset[str]],
) -> bool:
    evidence = frozenset().union(*evidence_sets) if evidence_sets else frozenset()
    required_by_kind: dict[str, set[str]] = {}
    evidence_by_kind: dict[str, set[str]] = {}
    for token in required:
        kind, _, identifier = token.partition(":")
        required_by_kind.setdefault(kind, set()).add(identifier)
    for token in evidence:
        kind, _, identifier = token.partition(":")
        evidence_by_kind.setdefault(kind, set()).add(identifier)
    return all(
        evidence_by_kind.get(kind, set()) == identifiers
        for kind, identifiers in required_by_kind.items()
    )


def _is_negated(text: str, match_start: int) -> bool:
    prefix = text[max(0, match_start - 24) : match_start]
    return bool(
        re.search(
            r"(?:\bnot\b|\bno\b|\bnever\b|\bwithout\b|\u5e76\u975e|\u4e0d\u662f|\u672a|\u4e0d)",
            prefix,
            re.IGNORECASE,
        )
    )


def _has_unsupported_numeric_contract(text: str) -> bool:
    value = str(text or "")
    identifier_spans = _numeric_identifier_spans(value)
    if _has_ambiguous_identifier_measurement(value):
        return True
    if _NONCANONICAL_UNICODE_NUMERIC_PATTERN.search(value) and re.search(
        r"\d|[\uff10-\uff19]", value
    ):
        return True
    if any(
        pattern.search(value)
        for pattern in (
            _TOLERANCE_PATTERN,
            _COMMA_DECIMAL_WITH_UNIT_PATTERN,
            _SCIENTIFIC_WITH_UNIT_PATTERN,
            _APPROXIMATE_WITH_UNIT_PATTERN,
            _SLASH_VALUE_WITH_UNIT_PATTERN,
            _NONCANONICAL_RANGE_PATTERN,
            _MULTIPLIED_EXPONENT_WITH_UNIT_PATTERN,
            _GROUPED_NUMBER_WITH_UNIT_PATTERN,
            _VOLTAGE_QUALIFIER_PATTERN,
            _DETACHED_UNIT_VALUE_PATTERN,
            _NEGATIVE_WORD_WITH_NUMBER_PATTERN,
            _ARTICLE_WITH_MEASUREMENT_PATTERN,
        )
    ):
        return True
    for match in _ANY_COMPARISON_WITH_UNIT_PATTERN.finditer(value):
        if _span_is_consumed(match.span("value"), identifier_spans):
            continue
        if match.group("unit").lower() not in _SUPPORTED_UNIT_TOKENS:
            return True
        if match.group("value").startswith(("-", "+", "\u2212")):
            return True
    for match in _ANY_RANGE_PATTERN.finditer(value):
        if _span_is_consumed(match.span("lower"), identifier_spans) and _span_is_consumed(
            match.span("upper"), identifier_spans
        ):
            continue
        units = (match.group("lower_unit"), match.group("upper_unit"))
        if any(unit and unit.lower() not in _SUPPORTED_UNIT_TOKENS for unit in units):
            return True
        if match.group("lower").startswith(("-", "+", "\u2212")) or match.group("upper").startswith(
            ("-", "+", "\u2212")
        ):
            return True
    _, canonical_spans = _extract_numeric_constraints_with_spans(value)
    if any(
        not _span_is_consumed(match.span(), canonical_spans)
        and not _span_is_consumed(match.span(), identifier_spans)
        for match in _MEASUREMENT_UNIT_LEXEME_PATTERN.finditer(value)
    ):
        return True
    if _FREQUENCY_QUANTITY_PATTERN.search(value) and _SCOPE_OBLIGATION_PATTERN.search(value):
        return True
    if not _all_numeric_tokens_are_consumed(value, canonical_spans):
        return True
    if any(_canonical_span_has_unsafe_context(value, span) for span in canonical_spans):
        return True
    for match in _ANY_VALUE_WITH_GENERIC_UNIT_PATTERN.finditer(value):
        if _span_is_consumed(match.span("value"), identifier_spans):
            continue
        if not _span_is_consumed(match.span(), canonical_spans):
            return True
    for match in _ATTACHED_SHORT_UNIT_PATTERN.finditer(value):
        if _span_is_consumed(match.span("value"), identifier_spans):
            continue
        if not _span_is_consumed(match.span(), canonical_spans):
            return True
    for match in _SEPARATED_SHORT_UNIT_PATTERN.finditer(value):
        unit = match.group("unit").lower()
        if (
            unit in _NON_UNIT_WORDS
            or _span_is_consumed(match.span("value"), identifier_spans)
            or _numeric_value_is_identifier(value, match.start())
        ):
            continue
        if not _span_is_consumed(match.span(), canonical_spans):
            return True
    return False


def _span_is_consumed(
    span: tuple[int, int],
    canonical_spans: Sequence[tuple[int, int]],
) -> bool:
    start, end = span
    return any(canonical_start <= start and end <= canonical_end for canonical_start, canonical_end in canonical_spans)


def _numeric_value_is_identifier(text: str, value_start: int) -> bool:
    prefix = text[max(0, value_start - 32) : value_start]
    return bool(_IDENTIFIER_PREFIX_PATTERN.search(prefix))


def _all_numeric_tokens_are_consumed(
    text: str,
    canonical_spans: Sequence[tuple[int, int]],
) -> bool:
    identifier_spans = _numeric_identifier_spans(text)
    for match in _NUMERIC_TOKEN_PATTERN.finditer(text):
        if _span_is_consumed(match.span(), canonical_spans):
            continue
        if _span_is_consumed(match.span(), identifier_spans):
            continue
        return False
    for index, character in enumerate(text):
        if not unicodedata.category(character).startswith("N"):
            continue
        span = (index, index + 1)
        if _span_is_consumed(span, canonical_spans) or _span_is_consumed(span, identifier_spans):
            continue
        return False
    for pattern in (_ENGLISH_NUMBER_WORD_PATTERN, _CHINESE_NUMBER_WORD_PATTERN):
        for match in pattern.finditer(text):
            if _span_is_consumed(match.span(), identifier_spans):
                continue
            return False
    return True


def _text_has_numeric_token(text: str) -> bool:
    if _NUMERIC_TOKEN_PATTERN.search(text):
        return True
    if _ENGLISH_NUMBER_WORD_PATTERN.search(text) or _CHINESE_NUMBER_WORD_PATTERN.search(text):
        return True
    return any(unicodedata.category(character).startswith("N") for character in text)


def _numeric_identifier_spans(text: str) -> tuple[tuple[int, int], ...]:
    patterns = (
        _IDENTIFIER_WITH_NUMBER_PATTERN,
        _CHINESE_IDENTIFIER_WITH_NUMBER_PATTERN,
        _CHINESE_REVERSE_IDENTIFIER_PATTERN,
        _CHINESE_SUFFIX_REFERENCE_SCOPE_PATTERN,
        _CHINESE_PREFIX_REFERENCE_SCOPE_PATTERN,
        _ROMAN_IDENTIFIER_PATTERN,
        _ROMAN_REFERENCE_SCOPE_PATTERN,
        _WORD_IDENTIFIER_PATTERN,
        _REVERSE_WORD_IDENTIFIER_PATTERN,
        _REFERENCE_IDENTIFIER_PATTERN,
        _ISO_EXTENDED_SCOPE_PATTERN,
        _SHORT_VERSION_SCOPE_PATTERN,
        _WORD_REFERENCE_SCOPE_PATTERN,
        _LEADING_SECTION_PATTERN,
        _YEAR_IDENTIFIER_PATTERN,
        _DATE_IDENTIFIER_PATTERN,
    )
    spans: list[tuple[int, int]] = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            if _identifier_span_is_measurement(
                text,
                match.span(),
                unknown_follower_is_measurement=pattern
                in {
                    _IDENTIFIER_WITH_NUMBER_PATTERN,
                    _CHINESE_IDENTIFIER_WITH_NUMBER_PATTERN,
                    _CHINESE_REVERSE_IDENTIFIER_PATTERN,
                    _ROMAN_IDENTIFIER_PATTERN,
                    _WORD_IDENTIFIER_PATTERN,
                    _REVERSE_WORD_IDENTIFIER_PATTERN,
                    _LEADING_SECTION_PATTERN,
                },
            ):
                continue
            spans.append(match.span())
    return tuple(spans)


def _identifier_span_is_measurement(
    text: str,
    span: tuple[int, int],
    *,
    unknown_follower_is_measurement: bool,
) -> bool:
    suffix = text[span[1] : min(len(text), span[1] + 32)]
    if _MEASUREMENT_UNIT_AFTER_IDENTIFIER_PATTERN.match(suffix):
        return True
    unwrapped = re.sub(
        r"^\s*(?:[-\u2010-\u2015:/\\|\"'\u2018\u2019\u201c\u201d`]\s*)*"
        r"(?:[\(\[\{\uff08\u3010]\s*)*",
        "",
        suffix,
    )
    follower = re.match(r"(?P<word>[A-Za-z]+)", unwrapped)
    if follower:
        if not unknown_follower_is_measurement:
            return False
        return follower.group("word").casefold() not in _IDENTIFIER_SAFE_FOLLOWERS
    chinese_follower = re.match(r"(?P<word>[\u3400-\u9fff]+)", unwrapped)
    if not chinese_follower or not unknown_follower_is_measurement:
        return False
    return not chinese_follower.group("word").startswith(_CHINESE_IDENTIFIER_SAFE_FOLLOWERS)


def _has_ambiguous_identifier_measurement(text: str) -> bool:
    strict_patterns = (
        _IDENTIFIER_WITH_NUMBER_PATTERN,
        _CHINESE_IDENTIFIER_WITH_NUMBER_PATTERN,
        _CHINESE_REVERSE_IDENTIFIER_PATTERN,
        _ROMAN_IDENTIFIER_PATTERN,
        _WORD_IDENTIFIER_PATTERN,
        _REVERSE_WORD_IDENTIFIER_PATTERN,
        _LEADING_SECTION_PATTERN,
    )
    return any(
        _identifier_span_is_measurement(
            text,
            match.span(),
            unknown_follower_is_measurement=True,
        )
        for pattern in strict_patterns
        for match in pattern.finditer(text)
    )


def _canonical_span_has_unsafe_context(text: str, span: tuple[int, int]) -> bool:
    start, end = span
    prefix = text[max(0, start - 48) : start]
    suffix = text[end : min(len(text), end + 96)]
    prefix_tail = prefix.rstrip()
    while prefix_tail:
        previous = prefix_tail[-1]
        if previous in ")]\uFF09\u3011}":
            prefix_tail = prefix_tail[:-1].rstrip()
            continue
        if unicodedata.category(previous) in {"Mn", "Me", "Cf"}:
            prefix_tail = prefix_tail[:-1].rstrip()
            continue
        break
    if prefix_tail:
        previous = prefix_tail[-1]
        previous_category = unicodedata.category(previous)
        previous_name = unicodedata.name(previous, "")
        if previous != "=" and (
            previous_category == "Pd"
            or previous_category.startswith("S")
            or "MINUS" in previous_name
            or "HYPHEN" in previous_name
        ):
            return True
    if re.search(r"[+\-\u2212\uff0d\ufe63]\s*$", prefix):
        return True
    if re.search(r"\d\s*[.\u2026\u22ef]+\s*$", prefix):
        return True
    if re.match(
        r"\s*(?:\u00b1|\+/-|/|[+\-\u2212\uff0d\ufe63]\s*\d|"
        r"[\(\[{{\uff08]\s*[+\-\u2212\uff0d\ufe63])",
        suffix,
    ):
        return True
    if re.match(
        r"\s*[\(\[\{\uff08\u3010]*\s*(?:[-.]\s*)?"
        r"(?:a\.?\s*c\.?|d\.?\s*c\.?|rms|peak|pk)\b",
        suffix,
        re.IGNORECASE,
    ):
        return True
    suffix_head = suffix.lstrip()
    while suffix_head and suffix_head[0] in "([{\uff08\u3010":
        suffix_head = suffix_head[1:].lstrip()
    if suffix_head:
        following = suffix_head[0]
        following_category = unicodedata.category(following)
        if (
            following == "_"
            or following_category.startswith("S")
            or following_category in {"Mn", "Me", "Cf"}
        ):
            return True
    qualifier_suffix = re.split(
        r"(?:[;,.!?\n\u3002\uff0c\uff1b\uff01\uff1f]|\b(?:and|but|while|whereas)\b)",
        suffix,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    for character in qualifier_suffix:
        category = unicodedata.category(character)
        name = unicodedata.name(character, "")
        if (
            character == "_"
            or category == "Pd"
            or category.startswith("S")
            or "MINUS" in name
            or "HYPHEN" in name
        ):
            return True
    return False


def _overlaps(span: tuple[int, int], occupied: Sequence[tuple[int, int]]) -> bool:
    start, end = span
    return any(start < occupied_end and occupied_start < end for occupied_start, occupied_end in occupied)
