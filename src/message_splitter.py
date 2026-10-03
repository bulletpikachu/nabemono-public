"""Split a model reply into the Discord messages the bot sends.

The bot texts in bursts: each sentence of a reply becomes its own message.
Sentence ends are ambiguous in lowercase chat ("model no. 5", "e.g. this",
"j. k. rowling"). Cutting a sentence in half is a visible bug, while leaving
two sentences in one message is harmless, so ambiguous spots never split.

1. Fenced code blocks are sent intact. As in Discord, the next ``` closes a
   block, and an unclosed block is closed at the end of the reply.
2. Every other line is split after a run of . ! ? or … followed by
   whitespace, unless the run is an ellipsis, follows an abbreviation,
   initial or list number, or sits inside inline code, a spoiler, brackets,
   quotes or emphasis.
3. One trailing period is dropped from each message. Ellipses are kept.
4. Messages over the length limit are cut at whitespace, and long code
   blocks are cut between lines with their fences reopened.
5. A reply that would take more than MAX_MESSAGES_PER_REPLY messages is sent
   one line per message instead, and failing that, with lines packed together.
"""

import re
from itertools import accumulate
from typing import NamedTuple


MAX_MESSAGE_LENGTH = 1900
MAX_MESSAGES_PER_REPLY = 10
MAX_DISCORD_TOKEN_LENGTH = 100
FENCE = "```"

# Words whose trailing period never ends a sentence. Dotted forms such as
# "e.g." and "u.s." are recognised by INITIALISM_RE instead.
ABBREVIATIONS = frozenset("""
    mr mrs ms dr prof sr jr st mt ft gen capt lt sgt rev hon gov sen rep pres
    etc vs cf al approx viz esp incl misc dept est govt inc ltd co corp bros
    ave blvd rd univ assn
    jan feb mar apr jun jul aug sep sept oct nov dec
    mon tue tues wed thu thur thurs fri sat sun
""".split())
# Words that only continue the sentence before a number ("no. 5", "pg. 12").
# Elsewhere they are ordinary words ("i said no. then he left").
NUMBER_ABBREVIATIONS = frozenset("""
    no nos vol vols ch chap pg pp fig figs art sec ep ed pt op ver ref eq tbl
""".split())
# Lone letters that are usually words ("love u.", "so am i.") and only count
# as initials next to another initial or as a list marker.
PRONOUN_LETTERS = frozenset("iu")

TERMINATOR_RUN_RE = re.compile(r"(?<![.!?…])[.!?…]+(?=\s)")
INITIALISM_RE = re.compile(r"[a-z]{1,2}(?:\.[a-z]{1,2})+\.", re.IGNORECASE)
ENUMERATION_NUMBER_RE = re.compile(r"(?<!\S)(\d{1,3})\.(?=\s+\S)")
BACKTICK_RUN_RE = re.compile(r"`+")
BRACKET_RE = re.compile(r"[()\[\]{}“”]")
SINGLE_QUOTE_RE = re.compile(r"['‘’]")
EMPHASIS_RE = re.compile(r"\*\*|__|~~|(?<!\*)\*(?!\*)")
LANGUAGE_TAG_RE = re.compile(r"[\w+#.-]{0,32}")
# Markdown heading, quote and bullet markers before a line's first word.
LINE_PREFIX_RE = re.compile(r"\s*(?:(?:#{1,6}|>{1,3}|[-*+])\s+)*")

LEADING_PUNCTUATION = "([{\"'“‘*_~`|<>"
OPENING_BRACKET_FOR = {")": "(", "]": "[", "}": "{", "”": "“"}
QUOTE_CLOSE_FOLLOWERS = ".,!?;:)]}…"


class _Code(NamedTuple):
    text: str


class _Line(NamedTuple):
    sentences: tuple[str, ...]


class _LineInfo(NamedTuple):
    text: str
    first_word: int
    enumeration: frozenset[int]


def split_reply(text: str) -> list[str]:
    """Return the Discord messages to send for a model reply, in order."""
    blocks = _parse_blocks(text)
    messages = _render(blocks, by_sentence=True)
    if len(messages) > MAX_MESSAGES_PER_REPLY:
        messages = _render(blocks, by_sentence=False)
    if len(messages) > MAX_MESSAGES_PER_REPLY:
        messages = _pack(messages, "\n")
    return messages


def _parse_blocks(text: str) -> list[_Code | _Line]:
    blocks = []
    for is_code, segment in _segment_fences(text):
        if is_code:
            blocks.append(_Code(segment))
            continue
        for line in segment.splitlines():
            sentences = tuple(piece.strip() for piece in _split_line(line) if piece.strip())
            if sentences:
                blocks.append(_Line(sentences))
    return blocks


def _render(blocks: list[_Code | _Line], by_sentence: bool) -> list[str]:
    messages = []
    for block in blocks:
        if isinstance(block, _Code):
            messages.extend(_chunk_code(block.text))
        elif by_sentence:
            for sentence in block.sentences:
                messages.extend(_prose_messages((sentence,)))
        else:
            messages.extend(_prose_messages(block.sentences))
    return messages


def _prose_messages(sentences: tuple[str, ...]) -> list[str]:
    pieces = [chunk for sentence in sentences for chunk in _chunk_text(sentence)]
    finished = (_finish(piece) for piece in _pack(pieces, " "))
    return [message for message in finished if message]


def _pack(pieces: list[str], separator: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Greedily join consecutive pieces while the result fits in the limit."""
    groups = []
    for piece in pieces:
        if groups and groups[-1][1] + len(separator) + len(piece) <= limit:
            parts, size = groups[-1]
            groups[-1] = (parts + [piece], size + len(separator) + len(piece))
        else:
            groups.append(([piece], len(piece)))
    return [separator.join(parts) for parts, _ in groups]


def _finish(message: str) -> str:
    """Trim a prose message and drop its final period."""
    message = message.strip()
    if not message.replace(".", "").strip():
        return message if message.count(".") > 1 else ""
    if message.endswith(".") and not message.endswith("..") and not _ends_with_initialism(message):
        return message[:-1].rstrip()
    return message


def _ends_with_initialism(message: str) -> bool:
    last_token = message.rsplit(None, 1)[-1].lstrip(LEADING_PUNCTUATION)
    return INITIALISM_RE.fullmatch(last_token) is not None


# ---------------------------------------------------------------- code fences

def _segment_fences(text: str) -> list[tuple[bool, str]]:
    """Split text into (is_code, segment) pairs, closing an unclosed fence."""
    segments = []
    position = 0
    while (start := text.find(FENCE, position)) != -1:
        segments.append((False, text[position:start]))
        close = text.find(FENCE, start + len(FENCE))
        if close == -1:
            block = text[start:]
            closing = FENCE if block.endswith("\n") else "\n" + FENCE
            segments.append((True, block + closing))
            return segments
        segments.append((True, text[start : close + len(FENCE)]))
        position = close + len(FENCE)
    segments.append((False, text[position:]))
    return segments


def _chunk_code(block: str) -> list[str]:
    """Cut a code block between lines, reopening the fence in every chunk."""
    header, body = _split_code_block(block)
    if not body.strip():
        return []
    if len(block) <= MAX_MESSAGE_LENGTH:
        return [block]
    budget = MAX_MESSAGE_LENGTH - len(header) - len(FENCE) - 2
    lines = [piece for line in body.split("\n") for piece in _slices(line, budget)]
    return [f"{header}\n{chunk}\n{FENCE}" for chunk in _pack(lines, "\n", budget)]


def _split_code_block(block: str) -> tuple[str, str]:
    """Return the opening fence with its language tag, and the code inside."""
    inner = block[len(FENCE) : -len(FENCE)]
    first_line, newline, rest = inner.partition("\n")
    if newline and LANGUAGE_TAG_RE.fullmatch(first_line):
        return FENCE + first_line, rest.removesuffix("\n")
    return FENCE, inner.removesuffix("\n")


def _slices(text: str, size: int) -> list[str]:
    return [text[start : start + size] for start in range(0, max(len(text), 1), size)]


# ---------------------------------------------------------------- sentences

def _split_line(line: str) -> list[str]:
    protected = _protected_mask(line)
    info = _LineInfo(
        text=line,
        first_word=LINE_PREFIX_RE.match(line).end(),
        enumeration=frozenset(int(number) for number in ENUMERATION_NUMBER_RE.findall(line)),
    )
    pieces = []
    start = 0
    for run in TERMINATOR_RUN_RE.finditer(line):
        if protected[run.start()] or _is_false_boundary(info, run):
            continue
        pieces.append(line[start : run.end()])
        start = run.end()
    pieces.append(line[start:])
    return pieces


def _is_false_boundary(info: _LineInfo, run: re.Match) -> bool:
    """Whether a terminator run followed by whitespace still continues the sentence."""
    line, punctuation = info.text, run.group()
    if ".." in punctuation or "…" in punctuation:
        return True
    word_start = _word_start(line, run.start())
    if word_start == run.start():
        return True  # detached dots, as in "wait . . . what"
    if punctuation != ".":
        return False
    token = line[word_start : run.start()]
    word = token.lstrip(LEADING_PUNCTUATION).lower()
    if word in ABBREVIATIONS or INITIALISM_RE.fullmatch(word + "."):
        return True
    if word in NUMBER_ABBREVIATIONS:
        following = _next_token(line, run.end())
        return following[:1].isdigit() or following.startswith("#")
    if len(word) == 1 and word.isalpha():
        return _is_initial(info, word, word_start, run.end())
    if token.isdecimal():
        return word_start == info.first_word or _is_enumerated(token, info.enumeration)
    return False


def _is_initial(info: _LineInfo, letter: str, word_start: int, run_end: int) -> bool:
    if letter not in PRONOUN_LETTERS or word_start == info.first_word:
        return True
    neighbours = (_previous_token(info.text, word_start), _next_token(info.text, run_end))
    return any(_is_lone_initial(token) for token in neighbours)


def _is_lone_initial(token: str) -> bool:
    core = token.lstrip(LEADING_PUNCTUATION)
    return len(core) == 2 and core[0].isalpha() and core[1] == "."


def _is_enumerated(number: str, enumeration: frozenset[int]) -> bool:
    if len(number) > 3:
        return False
    value = int(number)
    return value - 1 in enumeration or value + 1 in enumeration


def _word_start(text: str, end: int) -> int:
    start = end
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    return start


def _previous_token(text: str, end: int) -> str:
    while end > 0 and text[end - 1].isspace():
        end -= 1
    return text[_word_start(text, end) : end]


def _next_token(text: str, start: int) -> str:
    while start < len(text) and text[start].isspace():
        start += 1
    end = start
    while end < len(text) and not text[end].isspace():
        end += 1
    return text[start:end]


# ---------------------------------------------------------------- protected spans

def _protected_mask(line: str) -> list[bool]:
    """Mark characters inside spans that a sentence break must not cut."""
    code_spans = _code_spans(line)
    in_code = _coverage(len(line), code_spans)
    spans = [
        *code_spans,
        *_paired_spans(line, "||", in_code),
        *_paired_spans(line, '"', in_code),
        *_bracket_spans(line, in_code),
        *_single_quote_spans(line, in_code),
        *_emphasis_spans(line, in_code),
    ]
    return _coverage(len(line), spans)


def _coverage(length: int, spans: list[tuple[int, int]]) -> list[bool]:
    """Mark every index covered by a half-open span, in linear time."""
    depth_changes = [0] * (length + 1)
    for start, end in spans:
        depth_changes[start] += 1
        depth_changes[end] -= 1
    return [depth > 0 for depth in accumulate(depth_changes[:length])]


def _code_spans(line: str) -> list[tuple[int, int]]:
    """Inline code: a backtick run closed by the next run of the same width."""
    runs = [match.span() for match in BACKTICK_RUN_RE.finditer(line)]
    next_same_width = [None] * len(runs)
    latest_by_width = {}
    for index in range(len(runs) - 1, -1, -1):
        width = runs[index][1] - runs[index][0]
        next_same_width[index] = latest_by_width.get(width)
        latest_by_width[width] = index
    spans = []
    index = 0
    while index < len(runs):
        closing = next_same_width[index]
        if closing is None:
            index += 1
            continue
        spans.append((runs[index][0], runs[closing][1]))
        index = closing + 1
    return spans


def _paired_spans(line: str, delimiter: str, in_code: list[bool]) -> list[tuple[int, int]]:
    starts = [
        match.start()
        for match in re.finditer(re.escape(delimiter), line)
        if not in_code[match.start()]
    ]
    # An unpaired last delimiter protects nothing.
    pairs = zip(starts[::2], starts[1::2], strict=False)
    return [(start, end + len(delimiter)) for start, end in pairs]


def _bracket_spans(line: str, in_code: list[bool]) -> list[tuple[int, int]]:
    stack = []
    open_counts = dict.fromkeys(OPENING_BRACKET_FOR.values(), 0)
    spans = []
    for match in BRACKET_RE.finditer(line):
        index, char = match.start(), match.group()
        if in_code[index]:
            continue
        if char in open_counts:
            stack.append((char, index))
            open_counts[char] += 1
        elif open_counts[OPENING_BRACKET_FOR[char]]:
            wanted = OPENING_BRACKET_FOR[char]
            opener, start = stack.pop()
            open_counts[opener] -= 1
            while opener != wanted:
                opener, start = stack.pop()
                open_counts[opener] -= 1
            spans.append((start, index + 1))
    return spans


def _single_quote_spans(line: str, in_code: list[bool]) -> list[tuple[int, int]]:
    """Single-quoted text, telling quotes from apostrophes by what surrounds them."""
    spans = []
    start = None
    for match in SINGLE_QUOTE_RE.finditer(line):
        index, char = match.start(), match.group()
        if in_code[index]:
            continue
        before = line[index - 1] if index > 0 else " "
        after = line[index + 1] if index + 1 < len(line) else " "
        if start is None:
            if char != "’" and (before.isspace() or before in "([{") and not after.isspace():
                start = index
        elif char != "‘" and not before.isspace() and (after.isspace() or after in QUOTE_CLOSE_FOLLOWERS):
            spans.append((start, index + 1))
            start = None
    return spans


def _emphasis_spans(line: str, in_code: list[bool]) -> list[tuple[int, int]]:
    """Markdown emphasis: an opener before a word, closed after a word."""
    spans = []
    open_at = {}
    for match in EMPHASIS_RE.finditer(line):
        if in_code[match.start()]:
            continue
        delimiter = match.group()
        before = line[match.start() - 1] if match.start() > 0 else " "
        after = line[match.end()] if match.end() < len(line) else " "
        if delimiter not in open_at:
            if not after.isspace():
                open_at[delimiter] = match.start()
        elif not before.isspace():
            spans.append((open_at.pop(delimiter), match.end()))
    return spans


# ---------------------------------------------------------------- length limit

def _chunk_text(text: str) -> list[str]:
    """Cut a one-line prose message into pieces that each fit in a message."""
    if len(text) <= MAX_MESSAGE_LENGTH:
        return [text]
    protected = _protected_mask(text)
    chunks = []
    start = 0
    while len(text) - start > MAX_MESSAGE_LENGTH:
        cut = _cut_index(text, start, protected)
        chunks.append(text[start:cut].rstrip())
        start = cut
        while start < len(text) and text[start].isspace():
            start += 1
    chunks.append(text[start:])
    return [chunk for chunk in chunks if chunk]


def _cut_index(text: str, start: int, protected: list[bool]) -> int:
    """Pick where to end the chunk starting at start: the last whitespace
    that fits, preferably outside protected spans, else a hard cut."""
    limit = start + MAX_MESSAGE_LENGTH
    protected_whitespace = None
    for index in range(limit, start, -1):
        if not text[index].isspace():
            continue
        if not protected[index]:
            return index
        if protected_whitespace is None:
            protected_whitespace = index
    if protected_whitespace is not None:
        return protected_whitespace
    return _hard_cut_index(text, start, limit)


def _hard_cut_index(text: str, start: int, limit: int) -> int:
    """Cut at the limit, but before a Discord token like <:emoji:id> it would split."""
    token_start = text.rfind("<", max(start + 1, limit - MAX_DISCORD_TOKEN_LENGTH), limit)
    if token_start != -1 and ">" not in text[token_start:limit]:
        return token_start
    return limit
