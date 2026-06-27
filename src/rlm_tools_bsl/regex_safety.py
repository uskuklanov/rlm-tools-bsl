"""Регэксп-guard против catastrophic backtracking (ReDoS) на входах grep.

Отдельный leaf-модуль (без внутренних импортов) — оба входа grep его импортируют
(``helpers.grep`` и ``bsl_helpers.safe_grep``). Вынесен отдельно как разделение
ответственности, НЕ из-за циклической зависимости (импорт между helpers/bsl_helpers
односторонний).
"""

from __future__ import annotations

# Сообщение об отклонении (общее для grep и safe_grep).
NESTED_QUANTIFIER_ERROR = (
    "Паттерн содержит вложенные неограниченные кванторы "
    "(риск catastrophic backtracking); упростите regex или "
    "используйте литерал/name_hint"
)


def _read_quantifier(pattern: str, i: int) -> tuple[str | None, int]:
    """В позиции *i* прочитать квантор, если он там есть.

    Возвращает (kind, length): kind ∈ {"unbounded" (+, *, {n,}, {0,}),
    "bounded" (?, {n}, {n,m}, {,m}), None (квантора нет)}. *length* учитывает
    хвостовой ленивый '?' / possessive '+' модификатор.
    """
    n = len(pattern)
    if i >= n:
        return (None, 0)
    c = pattern[i]
    if c in "+*":
        length = 1
        if i + 1 < n and pattern[i + 1] in "?+":
            length += 1
        return ("unbounded", length)
    if c == "?":
        length = 1
        if i + 1 < n and pattern[i + 1] in "?+":
            length += 1
        return ("bounded", length)
    if c == "{":
        close = pattern.find("}", i + 1)
        if close == -1:
            return (None, 0)  # литеральная '{', не квантор
        body = pattern[i + 1 : close]
        if not body:
            return (None, 0)  # '{}' — литерал
        if "," in body:
            if body.count(",") != 1:
                return (None, 0)
            lo, hi = body.split(",")
            if (lo and not lo.isdigit()) or (hi and not hi.isdigit()):
                return (None, 0)
            kind: str | None = "unbounded" if hi == "" else "bounded"
        else:
            if not body.isdigit():
                return (None, 0)
            kind = "bounded"
        length = close - i + 1
        if close + 1 < n and pattern[close + 1] in "?+":
            length += 1
        return (kind, length)
    return (None, 0)


def has_catastrophic_nesting(pattern: str) -> bool:
    r"""True ⇔ pattern содержит неограниченно-квантифицированную группу, тело которой
    содержит неограниченный квантор: ``(...+)+``, ``(...*)*``, ``(...+)*``,
    ``(...*)+``, ``(...{n,})+`` (вкл. non-capturing/lookaround ``(?:…)+`` / ``(?=…)+``).
    Это классический exponential-ReDoS (catastrophic backtracking).

    Намеренно НЕ блокирует bounded inner-кванторы (``{n}``/``{n,m}``/``?``) — они
    линейны: ``(\d{4})+``, ``(\w{2,4})+``, ``(\d{3}-?)+`` разрешены. Экранированные
    скобки (``\(…\)``) и скобки/кванторы внутри класса символов (``[(+)]``) группой
    не считаются.

    Ожидаемые отклонения (это РЕАЛЬНЫЙ ReDoS из-за перекрытия классов, не баг):
    ``(\w+\s*)+``, ``(\d+\D*)+`` и т.п. — guard их блокирует намеренно.

    Это эвристика по СТРУКТУРЕ, НЕ полноценный ReDoS-детектор: alternation-overlap
    (``(a|a)*``) и т.п. не ловятся. Настоящий wall-clock-kill требует процессной
    изоляции (см. docs/ARCHITECTURE.md про границы песочницы).
    """
    if not pattern:
        return False
    n = len(pattern)
    stack: list[bool] = []  # на группу: встречен ли неограниченный квантор в её теле
    i = 0
    while i < n:
        c = pattern[i]
        if c == "\\":
            # экранированный атом \X (2 символа) — литерал, не метасимвол
            i = min(i + 2, n)
            quant, qlen = _read_quantifier(pattern, i)
            if quant == "unbounded" and stack:
                stack[-1] = True
            i += qlen
            continue
        if c == "[":
            # класс символов — '(' '+' ')' внутри НЕ образуют группу/квантор
            j = i + 1
            if j < n and pattern[j] == "^":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1  # ведущий ']' — литеральный член класса
            while j < n and pattern[j] != "]":
                j += 2 if pattern[j] == "\\" else 1
            i = j + 1 if j < n else n
            quant, qlen = _read_quantifier(pattern, i)
            if quant == "unbounded" and stack:
                stack[-1] = True
            i += qlen
            continue
        if c == "(":
            # новая группа (capturing / (?:…) / (?=…) / (?P<name>…) — все group-frame;
            # префиксные символы ?:=!<P обрабатываются как безобидные атомы ниже,
            # т.к. после '(' квантор не читается — '?' в (?:…) не примут за квантор).
            stack.append(False)
            i += 1
            continue
        if c == ")":
            frame = stack.pop() if stack else False
            i += 1
            quant, qlen = _read_quantifier(pattern, i)
            i += qlen
            if quant == "unbounded" and frame:
                # неограниченно-квантифицированная группа, ТЕЛО которой содержит
                # неограниченный квантор → catastrophic.
                return True
            if stack and (quant == "unbounded" or frame):
                # Распространяем факт «тело содержит неограниченный квантор» в ОБЪЕМЛЮЩУЮ
                # группу: либо сама группа неограниченно-квантифицирована (она —
                # неограниченный элемент родителя), либо её тело уже содержало
                # неограниченный квантор ТРАНЗИТИВНО — даже через bounded-квантор
                # подгруппы (`((a+){2})+` — codex). Bounded inner-кванторы атомов сами
                # по себе frame НЕ ставят, поэтому `(\d{4})+` остаётся линейным.
                stack[-1] = True
            continue
        # обычный атом-символ
        i += 1
        quant, qlen = _read_quantifier(pattern, i)
        if quant == "unbounded" and stack:
            stack[-1] = True
        i += qlen
    return False
