"""Parse and evaluate the existing download-filter language with Lark."""

import operator
import re
from datetime import datetime
from typing import Any, Optional, Tuple

from lark import Lark, Transformer_NonRecursive, UnexpectedInput, v_args
from lark.exceptions import VisitError

from utils.format import get_byte_from_str
from utils.meta_data import MetaData, NoneObj, ReString


_GRAMMAR = r"""
?start: NAME "=" expression -> legacy_equal
      | expression
?expression: disjunction
?disjunction: conjunction
            | disjunction OR conjunction -> binary
?conjunction: equality
            | conjunction AND equality -> binary
?equality: comparison
         | equality EQ comparison -> binary
?comparison: sum
           | sum COMP sum -> binary
?sum: product
    | sum (PLUS | MINUS) product -> binary
?product: unary
        | product MUL unary -> binary
?unary: atom
      | MINUS unary -> negative
?atom: NUMBER -> number
     | BYTE -> byte_size
     | TIME -> timestamp
     | STRING -> string
     | RESTRING -> regex_string
     | NAME -> name
     | "(" expression ")"
OR.2: "||" | /or(?![a-zA-Z0-9_])/
AND.2: "&&" | /and(?![a-zA-Z0-9_])/
EQ: "==" | "!="
COMP: ">=" | "<=" | ">" | "<"
PLUS: "+"
MINUS: "-"
MUL: "*" | "/"
TIME.3: /\d{4}-\d{1,2}-\d{1,2}[ ]+\d{1,2}:\d{1,2}:\d{1,2}/
BYTE.2: /\d+(B|KB|MB|GB|TB)/
RESTRING.2: /r'[^'\n]*'/
STRING: /'[^'\n]*'/
NAME: /[a-zA-Z_][a-zA-Z0-9_]*/
NUMBER: /\d+/
%ignore /[ \t\r\n]+/
"""

# Parsing has no mutable symbol table and writes no generated parser files.
# Each evaluation gets its own transformer and the calling filter's metadata.
_PARSER = Lark(_GRAMMAR, parser="lalr", lexer="basic")
_ARITHMETIC = {"+": operator.add, "-": operator.sub, "*": operator.mul, "/": operator.truediv}
_COMPARISON = {">": operator.gt, "<": operator.lt, ">=": operator.ge, "<=": operator.le}

MAX_FILTER_LENGTH = 4096
MAX_DOWNLOAD_FILTERS = 32
_TEXT_FIELDS = {"media_file_name", "file_name", "message_caption", "caption",
                "media_type", "file_extension", "sender_name"}


@v_args(inline=True)
class _Validator(Transformer_NonRecursive):
    """Check field types and literals without matching regexes against sample text."""

    def number(self, value):
        return int

    def byte_size(self, value):
        get_byte_from_str(str(value))
        return int

    def timestamp(self, value):
        try:
            datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            raise ValueError("Invalid filter date; use YYYY-MM-DD HH:MM:SS") from None
        return datetime

    def string(self, value):
        return str

    def regex_string(self, value):
        try:
            re.compile(str(value)[2:-1], re.MULTILINE)
        except re.error:
            raise ValueError("Invalid filter regular expression") from None
        return ReString

    def name(self, value):
        name = str(value)
        if name not in MetaData().data():
            raise ValueError(f"Undefined name {name}")
        return str if name in _TEXT_FIELDS else datetime if name == "message_date" else int

    def negative(self, operation, value):
        if value not in (int, float):
            raise ValueError("Filter arithmetic requires numbers")
        return value

    def legacy_equal(self, name, right):
        raise ValueError("Use == to compare filter values")

    def binary(self, left, operation, right):
        operation = str(operation)
        if operation in ("and", "&&", "or", "||"):
            if left is not bool or right is not bool:
                raise ValueError("Filter logical operators require comparisons")
            return bool
        if (left is not right and {left, right} != {str, ReString}
                and not (left is float and right is int)):
            raise ValueError("Filter operands must have compatible types")
        if operation in _ARITHMETIC:
            if left not in (int, float) or right not in (int, float):
                raise ValueError("Filter arithmetic requires numbers")
            return float if operation == "/" else left
        if operation in _COMPARISON and ReString in (left, right):
            raise ValueError("Regular expressions require == or !=")
        if left is ReString and right is ReString:
            raise ValueError("Compare a regular expression with a text field")
        return bool


def validate_download_filter(expression: str) -> str:
    """Validate one new editor rule without Telegram access or changing saved filters."""
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("Enter a filter expression")
    if len(expression) > MAX_FILTER_LENGTH:
        raise ValueError(f"Filter expression cannot exceed {MAX_FILTER_LENGTH} characters")
    expression = expression.strip()
    try:
        tree = _PARSER.parse(expression)
        result = _Validator().transform(tree)
        # Constant zero divisors are invalid even in an eager boolean branch.
        # Metadata-dependent arithmetic is checked when a message is processed.
        for binary in tree.find_data("binary"):
            right = binary.children[2]
            if (str(binary.children[1]) == "/" and not list(right.find_data("name"))
                    and _Evaluator({}).transform(right) == 0):
                raise ValueError("Filter division by zero")
    except UnexpectedInput as error:
        raise ValueError(f"Invalid filter syntax at line {error.line}, column {error.column}") from None
    except VisitError as error:
        if isinstance(error.orig_exc, ZeroDivisionError):
            raise ValueError("Filter division by zero") from None
        raise ValueError(str(error.orig_exc)) from None
    except ZeroDivisionError:
        raise ValueError("Filter division by zero") from None
    if result is not bool:
        raise ValueError("Filter expression must return true or false")
    return expression


@v_args(inline=True)
class _Evaluator(Transformer_NonRecursive):
    def __init__(self, names):
        super().__init__()
        self.names = names

    def number(self, value):
        return int(value)

    def byte_size(self, value):
        return get_byte_from_str(str(value))

    def timestamp(self, value):
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")

    def string(self, value):
        return str(value)[1:-1]

    def regex_string(self, value):
        return ReString(str(value)[2:-1])

    def name(self, value):
        name = str(value)
        try:
            return self.names[name]
        except KeyError as error:
            raise ValueError(f"Undefined name {name}") from error

    def negative(self, operation, value):
        return -value

    def legacy_equal(self, name, right):
        # A single '=' historically compares the literal name, without assigning
        # metadata. Retain this behavior for existing saved expressions.
        return self.binary(str(name), "==", right)

    def binary(self, left, operation, right):
        operation = str(operation)
        # Both operands have already been evaluated, as in the old parser.
        if operation in ("or", "||"):
            return left or right
        if operation in ("and", "&&"):
            return left and right
        self.check_type(left, right)
        if operation in _ARITHMETIC:
            left = 0 if isinstance(left, NoneObj) else left
            right = 0 if isinstance(right, NoneObj) else right
            return _ARITHMETIC[operation](left, right)
        # Keep missing-value and asymmetric type-check semantics unchanged.
        if isinstance(left, NoneObj) or isinstance(right, NoneObj):
            return True
        if left is None or right is None:
            return False
        if operation in _COMPARISON:
            return _COMPARISON[operation](left, right)
        if isinstance(right, ReString):
            if not isinstance(left, str):
                return 0
            matched = re.fullmatch(right.re_string, left, re.MULTILINE) is not None
        elif isinstance(left, ReString):
            if not isinstance(right, str):
                return 0
            matched = re.fullmatch(left.re_string, right, re.MULTILINE) is not None
        else:
            matched = left == right
        return matched if operation == "==" else not matched

    @staticmethod
    def check_type(left, right):
        if left is None or left is NoneObj or right is None or right is NoneObj:
            return
        if isinstance(left, str):
            if not isinstance(right, (str, ReString)):
                raise ValueError(f"{left} is str but {right} is not")
        elif isinstance(left, int):
            if not isinstance(right, int):
                raise ValueError(f"{left} is int but {right} is not")
        elif isinstance(left, bool):
            if not isinstance(right, bool):
                raise ValueError(f"{left} is bool but {right} is not")
        elif isinstance(left, datetime):
            if not isinstance(right, datetime):
                raise ValueError(f"{left} is datetime but {right} is not")


class BaseFilter:
    """Evaluate filter expressions against a caller-owned metadata map."""

    def __init__(self, debug: bool = False):
        self.names: dict = {}
        self.debug = debug

    def reset(self):
        """Clear the metadata before applying a different message."""
        self.names.clear()

    def exec(self, filter_str: str) -> Any:
        """Evaluate only the filter grammar, without executing Python code."""
        try:
            tree = _PARSER.parse(filter_str)
            result = _Evaluator(self.names).transform(tree)
        except UnexpectedInput as error:
            token = getattr(error, "token", None)
            if token is not None and token.type == "$END":
                raise ValueError("Syntax error at EOF") from error
            value = str(token) if token is not None else getattr(error, "char", "")
            raise ValueError(f"Syntax error at '{value}'") from error
        except VisitError as error:
            raise error.orig_exc from error
        if self.debug:
            print(result)
        return result


class Filter:
    """filter for telegram download"""

    def __init__(self):
        self.filter = BaseFilter()

    def set_meta_data(self, meta_data: MetaData):
        """Set meta data for filter"""
        self.filter.reset()
        self.filter.names = meta_data.data()

    def exec(self, filter_str: str) -> bool:
        """Exec filter str"""

        if self.filter.names:
            res = self.filter.exec(filter_str)
            if isinstance(res, bool):
                return res
            return False
        raise ValueError("meta data cannot be empty!")

    def check_filter(self, filter_str: str) -> Tuple[bool, Optional[str]]:
        """check filter str"""
        try:
            return not self.exec(filter_str) is None, None
        except Exception as e:
            return False, str(e)
