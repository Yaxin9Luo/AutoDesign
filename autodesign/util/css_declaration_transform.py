"""Deterministic CSS declaration-value transformation without runtime dependencies."""

from __future__ import annotations

import re


__all__ = (
    "find_declaration_list_hash_tokens",
    "find_stylesheet_hash_tokens",
    "transform_declaration_list_values",
    "transform_stylesheet_declaration_values",
)

DEFAULT_MAX_NESTING_DEPTH = 64


class _NestedRuleDetected(Exception):
    pass


class _CssDeclarationTransformer:
    def __init__(
        self,
        css: str,
        replacements: dict[str, str],
        *,
        max_nesting_depth: int,
    ) -> None:
        if max_nesting_depth < 1:
            raise ValueError("max_nesting_depth must be at least 1")
        self.css = str(css or "")
        self.length = len(self.css)
        self.index = 0
        self.max_nesting_depth = max_nesting_depth
        self.hash_replacements = {
            str(token).casefold(): str(replacement)
            for token, replacement in replacements.items()
            if str(token).startswith("#")
        }
        self.name_replacements = {
            str(token).casefold(): str(replacement)
            for token, replacement in replacements.items()
            if str(token) and not str(token).startswith("#")
        }
        self.semantic_hash_tokens: set[str] = set()

    def transform_stylesheet(self) -> str:
        transformed = self._parse_rule_list(closing_block=False, depth=0)
        if self.index != self.length:
            self._raise("unexpected trailing input")
        return transformed

    def transform_declaration_list(self) -> str:
        transformed = self._parse_declaration_list(
            closing_block=False,
            allow_nested_rules=False,
            depth=0,
        )
        if self.index != self.length:
            self._raise("unexpected trailing input")
        return transformed

    def _parse_rule_list(self, *, closing_block: bool, depth: int) -> str:
        out: list[str] = []
        while True:
            out.append(self._consume_trivia())
            if self.index >= self.length:
                if closing_block:
                    self._raise("unclosed rule block")
                return "".join(out)
            if self.css[self.index] == "}":
                if not closing_block:
                    self._raise("unexpected closing brace")
                out.append("}")
                self.index += 1
                return "".join(out)
            if self.css[self.index] == "@":
                out.append(self._parse_at_rule(depth=depth))
            else:
                out.append(self._parse_qualified_rule(depth=depth))

    def _parse_at_rule(self, *, depth: int) -> str:
        start = self.index
        self._consume_at_rule_name()
        _, terminator = self._consume_prelude(
            {";", "{"},
            "at-rule prelude",
            depth=depth,
        )
        if terminator == ";":
            self.index += 1
            return self.css[start:self.index]
        self.index += 1
        head = self.css[start:self.index]
        return head + self._parse_declaration_list(
            closing_block=True,
            allow_nested_rules=True,
            depth=self._next_depth(depth),
        )

    def _parse_qualified_rule(self, *, depth: int) -> str:
        start = self.index
        prelude, terminator = self._consume_prelude(
            {";", "{"},
            "qualified-rule prelude",
            depth=depth,
        )
        if terminator != "{" or not prelude.strip():
            self._raise("qualified rule must have a non-empty prelude and block")
        self.index += 1
        head = self.css[start:self.index]
        return head + self._parse_declaration_list(
            closing_block=True,
            allow_nested_rules=True,
            depth=self._next_depth(depth),
        )

    def _parse_declaration_list(
        self,
        *,
        closing_block: bool,
        allow_nested_rules: bool,
        depth: int,
    ) -> str:
        out: list[str] = []
        while True:
            out.append(self._consume_trivia())
            if self.index >= self.length:
                if closing_block:
                    self._raise("unclosed declaration block")
                return "".join(out)
            char = self.css[self.index]
            if char == "}":
                if not closing_block:
                    self._raise("unexpected closing brace in declaration list")
                out.append("}")
                self.index += 1
                return "".join(out)
            if char == ";":
                out.append(";")
                self.index += 1
                continue
            if char == "@":
                if not allow_nested_rules:
                    self._raise("at-rules are not valid in an inline declaration list")
                out.append(self._parse_at_rule(depth=depth))
                continue
            out.append(self._parse_declaration_or_nested_rule(
                closing_block=closing_block,
                allow_nested_rules=allow_nested_rules,
                depth=depth,
            ))

    def _parse_declaration_or_nested_rule(
        self,
        *,
        closing_block: bool,
        allow_nested_rules: bool,
        depth: int,
    ) -> str:
        start = self.index
        _, terminator = self._consume_prelude(
            {":", ";", "{"},
            "declaration name",
            depth=depth,
            allow_closing_brace=closing_block,
            allow_eof=not closing_block,
        )
        if terminator == "{" and allow_nested_rules:
            self.index = start
            return self._parse_qualified_rule(depth=depth)
        if terminator != ":":
            self._raise("declaration is missing a property/value colon")
        property_source = self.css[start:self.index]
        property_token = self._normalized_property_name(property_source)
        property_name = self._decoded_identifier(property_token)
        if not self._is_property_name(property_name):
            if allow_nested_rules:
                self.index = start
                return self._parse_qualified_rule(depth=depth)
            self._raise(f"invalid declaration property {property_token!r}")
        self.index += 1
        head = self.css[start:self.index]
        is_custom_property = property_name.startswith("--")
        try:
            value = self._parse_declaration_value(
                closing_block=closing_block,
                allow_curly_blocks=is_custom_property,
                detect_nested_rule=allow_nested_rules and not is_custom_property,
                depth=depth,
            )
        except _NestedRuleDetected:
            self.index = start
            return self._parse_qualified_rule(depth=depth)
        return head + value

    def _parse_declaration_value(
        self,
        *,
        closing_block: bool,
        allow_curly_blocks: bool,
        detect_nested_rule: bool,
        depth: int,
    ) -> str:
        out: list[str] = []
        # Each stack entry is the exact closer required at that component depth.
        expected_closers: list[str] = []
        while self.index < self.length:
            char = self.css[self.index]
            if self.css.startswith("/*", self.index):
                out.append(self._consume_comment())
                continue
            if char in {'"', "'"}:
                out.append(self._consume_string())
                continue
            if not expected_closers and char == ";":
                out.append(";")
                self.index += 1
                return "".join(out)
            if not expected_closers and char == "}":
                if not closing_block:
                    self._raise("unexpected closing brace in declaration value")
                return "".join(out)
            if char == "{" and not expected_closers and not allow_curly_blocks:
                if detect_nested_rule:
                    raise _NestedRuleDetected()
                self._raise("unexpected block in declaration value")
            if char in "([{":
                self._push_closer(
                    expected_closers,
                    {"(": ")", "[": "]", "{": "}"}[char],
                    depth=depth,
                )
                out.append(char)
                self.index += 1
                continue
            if char in ")]}":
                if not expected_closers or char != expected_closers[-1]:
                    self._raise(f"unexpected {char!r} in declaration value")
                expected_closers.pop()
                out.append(char)
                self.index += 1
                continue
            if char == "#":
                out.append(self._consume_hash_token(replace=True))
                continue
            if self._is_name_char(char):
                token = self._consume_name_token()
                semantic = self._decoded_identifier(token).casefold()
                if (
                    semantic == "url"
                    and self.index < self.length
                    and self.css[self.index] == "("
                ):
                    out.append(token)
                    out.append(self._consume_raw_parenthesized("URL", depth=depth))
                else:
                    out.append(self.name_replacements.get(semantic, token))
                continue
            out.append(char)
            self.index += 1
        if expected_closers:
            self._raise(f"unbalanced declaration value; expected {expected_closers[-1]!r}")
        if closing_block:
            self._raise("unclosed declaration block")
        return "".join(out)

    def _consume_prelude(
        self,
        terminators: set[str],
        context: str,
        *,
        depth: int,
        allow_closing_brace: bool = False,
        allow_eof: bool = False,
    ) -> tuple[str, str | None]:
        start = self.index
        # Each stack entry is the exact closer required at that prelude depth.
        expected_closers: list[str] = []
        while self.index < self.length:
            char = self.css[self.index]
            if self.css.startswith("/*", self.index):
                self._consume_comment()
                continue
            if char in {'"', "'"}:
                self._consume_string()
                continue
            if char == "\\":
                self._consume_escape()
                continue
            if not expected_closers and char in terminators:
                return self.css[start:self.index], char
            if not expected_closers and char == "}":
                if allow_closing_brace:
                    return self.css[start:self.index], "}"
                self._raise(f"unexpected closing brace in {context}")
            if char in "([":
                self._push_closer(
                    expected_closers,
                    {"(": ")", "[": "]"}[char],
                    depth=depth,
                )
                self.index += 1
                continue
            if char in ")]":
                if not expected_closers or char != expected_closers[-1]:
                    self._raise(f"unexpected {char!r} in {context}")
                expected_closers.pop()
                self.index += 1
                continue
            if char == "#":
                self._consume_hash_token(replace=False)
                continue
            self.index += 1
        if expected_closers:
            self._raise(f"unbalanced {context}; expected {expected_closers[-1]!r}")
        if allow_eof:
            return self.css[start:self.index], None
        self._raise(f"unterminated {context}")

    def _consume_at_rule_name(self) -> str:
        if self.css[self.index] != "@":
            self._raise("expected at-rule")
        self.index += 1
        start = self.index
        while self.index < self.length and self._is_name_char(self.css[self.index]):
            if self.css[self.index] == "\\":
                self._consume_escape()
            else:
                self.index += 1
        if self.index == start:
            self._raise("at-rule is missing a name")
        return self._decoded_identifier(self.css[start:self.index]).casefold()

    def _consume_trivia(self) -> str:
        start = self.index
        while self.index < self.length:
            if self.css[self.index].isspace():
                self.index += 1
            elif self.css.startswith("/*", self.index):
                self._consume_comment()
            else:
                break
        return self.css[start:self.index]

    def _consume_comment(self) -> str:
        start = self.index
        end = self.css.find("*/", self.index + 2)
        if end < 0:
            self._raise("unterminated comment")
        self.index = end + 2
        return self.css[start:self.index]

    def _consume_string(self) -> str:
        start = self.index
        quote = self.css[self.index]
        self.index += 1
        while self.index < self.length:
            char = self.css[self.index]
            if char == quote:
                self.index += 1
                return self.css[start:self.index]
            if char == "\\":
                self._consume_escape()
                continue
            if char in "\r\n\f":
                self._raise("unterminated quoted string")
            self.index += 1
        self._raise("unterminated quoted string")

    def _consume_escape(self) -> str:
        start = self.index
        self.index += 1
        if self.index >= self.length:
            self._raise("trailing escape")
        if (
            self.css[self.index] == "\r"
            and self.index + 1 < self.length
            and self.css[self.index + 1] == "\n"
        ):
            self.index += 2
            return self.css[start:self.index]
        if self.css[self.index] in "\n\r\f":
            self.index += 1
            return self.css[start:self.index]
        if self.css[self.index] in "0123456789abcdefABCDEF":
            consumed = 0
            while (
                self.index < self.length
                and consumed < 6
                and self.css[self.index] in "0123456789abcdefABCDEF"
            ):
                self.index += 1
                consumed += 1
            if self.index < self.length and self.css[self.index].isspace():
                if (
                    self.css[self.index] == "\r"
                    and self.index + 1 < self.length
                    and self.css[self.index + 1] == "\n"
                ):
                    self.index += 2
                else:
                    self.index += 1
            return self.css[start:self.index]
        self.index += 1
        return self.css[start:self.index]

    def _consume_hash_token(self, *, replace: bool) -> str:
        start = self.index
        self.index += 1
        while self.index < self.length and self._is_name_char(self.css[self.index]):
            if self.css[self.index] == "\\":
                self._consume_escape()
            else:
                self.index += 1
        token = self.css[start:self.index]
        semantic = "#" + self._decoded_identifier(token[1:])
        self.semantic_hash_tokens.add(semantic.upper())
        if replace:
            return self.hash_replacements.get(semantic.casefold(), token)
        return token

    def _consume_name_token(self) -> str:
        start = self.index
        while self.index < self.length and self._is_name_char(self.css[self.index]):
            if self.css[self.index] == "\\":
                self._consume_escape()
            else:
                self.index += 1
        return self.css[start:self.index]

    def _consume_raw_parenthesized(self, context: str, *, depth: int) -> str:
        start = self.index
        nested_depth = 0
        while self.index < self.length:
            char = self.css[self.index]
            if self.css.startswith("/*", self.index):
                self._consume_comment()
                continue
            if char in {'"', "'"}:
                self._consume_string()
                continue
            if char == "\\":
                self._consume_escape()
                continue
            if char == "(":
                nested_depth += 1
                self._check_component_depth(depth, nested_depth)
            elif char == ")":
                nested_depth -= 1
                if nested_depth == 0:
                    self.index += 1
                    return self.css[start:self.index]
                if nested_depth < 0:
                    self._raise(f"unexpected closing parenthesis in {context}")
            self.index += 1
        self._raise(f"unbalanced {context}; expected ')'")

    def _next_depth(self, depth: int) -> int:
        next_depth = depth + 1
        if next_depth > self.max_nesting_depth:
            self._raise(
                f"maximum nesting depth {self.max_nesting_depth} exceeded"
            )
        return next_depth

    def _push_closer(
        self,
        expected_closers: list[str],
        closer: str,
        *,
        depth: int,
    ) -> None:
        self._check_component_depth(depth, len(expected_closers) + 1)
        expected_closers.append(closer)

    def _check_component_depth(self, depth: int, component_depth: int) -> None:
        if depth + component_depth > self.max_nesting_depth:
            self._raise(
                f"maximum nesting depth {self.max_nesting_depth} exceeded"
            )

    @staticmethod
    def _normalized_property_name(value: str) -> str:
        return re.sub(r"/\*.*?\*/", "", value, flags=re.S).strip()

    @classmethod
    def _is_property_name(cls, value: str) -> bool:
        if not value or value in {"-", "--"}:
            return False
        if not cls._is_name_start(value[0]):
            return False
        return all(cls._is_decoded_name_char(char) for char in value[1:])

    @staticmethod
    def _is_name_start(char: str) -> bool:
        return char.isalpha() or char in "-_" or ord(char) >= 128

    @staticmethod
    def _is_decoded_name_char(char: str) -> bool:
        return char.isalnum() or char in "-_" or ord(char) >= 128

    @staticmethod
    def _is_name_char(char: str) -> bool:
        return char.isalnum() or char in "-_\\" or ord(char) >= 128

    @staticmethod
    def _decoded_identifier(value: str) -> str:
        decoded: list[str] = []
        index = 0
        while index < len(value):
            if value[index] != "\\":
                decoded.append(value[index])
                index += 1
                continue
            index += 1
            if index >= len(value):
                break
            if value[index] == "\r" and index + 1 < len(value) and value[index + 1] == "\n":
                index += 2
                continue
            if value[index] in "\n\r\f":
                index += 1
                continue
            if value[index] in "0123456789abcdefABCDEF":
                start = index
                while (
                    index < len(value)
                    and index - start < 6
                    and value[index] in "0123456789abcdefABCDEF"
                ):
                    index += 1
                codepoint = int(value[start:index], 16)
                decoded.append(
                    chr(codepoint)
                    if (
                        codepoint
                        and codepoint <= 0x10FFFF
                        and not 0xD800 <= codepoint <= 0xDFFF
                    )
                    else "\N{REPLACEMENT CHARACTER}"
                )
                if index < len(value) and value[index].isspace():
                    if (
                        value[index] == "\r"
                        and index + 1 < len(value)
                        and value[index + 1] == "\n"
                    ):
                        index += 2
                    else:
                        index += 1
                continue
            decoded.append(value[index])
            index += 1
        return "".join(decoded)

    def _raise(self, detail: str) -> None:
        raise ValueError(f"malformed CSS at offset {self.index}: {detail}")


def transform_stylesheet_declaration_values(
    css: str,
    replacements: dict[str, str],
    *,
    max_nesting_depth: int = DEFAULT_MAX_NESTING_DEPTH,
) -> str:
    return _CssDeclarationTransformer(
        css,
        replacements,
        max_nesting_depth=max_nesting_depth,
    ).transform_stylesheet()


def transform_declaration_list_values(
    css: str,
    replacements: dict[str, str],
    *,
    max_nesting_depth: int = DEFAULT_MAX_NESTING_DEPTH,
) -> str:
    return _CssDeclarationTransformer(
        css,
        replacements,
        max_nesting_depth=max_nesting_depth,
    ).transform_declaration_list()


def find_stylesheet_hash_tokens(
    css: str,
    *,
    max_nesting_depth: int = DEFAULT_MAX_NESTING_DEPTH,
) -> set[str]:
    parser = _CssDeclarationTransformer(
        css,
        {},
        max_nesting_depth=max_nesting_depth,
    )
    parser.transform_stylesheet()
    return set(parser.semantic_hash_tokens)


def find_declaration_list_hash_tokens(
    css: str,
    *,
    max_nesting_depth: int = DEFAULT_MAX_NESTING_DEPTH,
) -> set[str]:
    parser = _CssDeclarationTransformer(
        css,
        {},
        max_nesting_depth=max_nesting_depth,
    )
    parser.transform_declaration_list()
    return set(parser.semantic_hash_tokens)
