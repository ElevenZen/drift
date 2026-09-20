import re
from typing import Any, Iterable, List, Optional, Mapping, Union, Sequence

from .exceptions import ConfigError

try:
    import tomllib  # type: ignore[import-not-found, unused-ignore] # pyright: ignore[reportMissingImports]
    HAS_TOMLLIB = True
except ImportError:
    tomllib = None  # type: ignore[assignment]
    HAS_TOMLLIB = False


def get_first_from(
    data: Optional[Mapping[str, Any]],
    keys: Iterable[str],
    default: Any = None,
) -> Any:
    """Retrieves the first present value from a dictionary using an iterable of alternative key names.

    Args:
        data: Dictionary or mapping to search for keys.
        keys: Candidate keys in order of precedence (can be a generator, tuple, list, etc.).
        default: Fallback value if none of the candidate keys are present in data.

    Returns:
        The value of the first matching key in data, or default if no keys match.
    """
    if not isinstance(data, (dict, Mapping)):
        return default
    return next((data[key] for key in keys if key in data), default)


def get_nested_from(
    data: Optional[Mapping[str, Any]],
    keys: Union[str, Sequence[str]],
    default: Any = None,
    required: bool = False,
    is_table: bool = False,
    context: str = "configuration",
) -> Any:
    """Retrieves a nested value from a mapping given a dot-delimited key path or key sequence.

    Args:
        data: Mapping to traverse.
        keys: Dot-separated path string (e.g. "packages.enable") or sequence of keys.
        default: Fallback value if the nested path is missing and required is False.
        required: If True, raises ConfigError when the path does not exist.
        is_table: If True and the retrieved value is not a table/mapping, raises ConfigError.
        context: Context descriptor (e.g. "workspace configuration", "package configuration")
            used in error messages.

    Returns:
        The nested value at the specified key path, or `default` if not found.

    Raises:
        ConfigError: If required is True and the key path is missing, or if is_table is True
            and the found value is not a mapping.
    """
    path_str = keys if isinstance(keys, str) else ".".join(str(k) for k in keys)
    path_parts = [k.strip() for k in keys.split(".")] if isinstance(keys, str) else [str(k) for k in keys]

    if not isinstance(data, (dict, Mapping)):
        if required:
            raise ConfigError(f"Missing '[{path_str}]' section in {context}.")
        return default

    val: Any = data
    for key in path_parts:
        if not isinstance(val, (dict, Mapping)) or key not in val:
            if required:
                raise ConfigError(f"Missing '[{path_str}]' section in {context}.")
            return default
        val = val[key]

    if val is None:
        if required:
            raise ConfigError(f"Missing '[{path_str}]' section in {context}.")
        return default

    if is_table and not isinstance(val, (dict, Mapping)):
        raise ConfigError(f"'[{path_str}]' must be a TOML table.")

    return val


def validate_known_keys(
    data: Optional[Mapping[str, Any]],
    known_keys: Iterable[str],
    context: str = "",
    message_prefix: Optional[str] = None,
    suffix: str = "",
) -> None:
    """Validates that all keys in a mapping are within a set of known valid keys.

    Uses a functional filter to gather all unknown keys and raises a ConfigError
    reporting all unknown keys if any are found.

    Args:
        data: The dictionary or mapping to validate.
        known_keys: Iterable of allowed/known key names.
        context: Context descriptor (e.g. "[settings]", "requirements") used to build
            the error message prefix if message_prefix is not explicitly provided.
        message_prefix: Explicit prefix for the error message (e.g. "Unknown workspace option").
        suffix: Suffix appended to the error message (e.g. " for package 'foo'").

    Raises:
        ConfigError: If any keys in data are not in known_keys.
    """
    if not data or not isinstance(data, (dict, Mapping)):
        return

    known_set = set(known_keys)
    unknown_keys = list(filter(lambda k: k not in known_set, data.keys()))
    if not unknown_keys:
        return

    keys_str = ", ".join(f"'{k}'" for k in unknown_keys)
    if message_prefix is not None:
        prefix = message_prefix
    elif context:
        prefix = f"Unknown option under {context}"
    else:
        prefix = "Unknown option"

    raise ConfigError(f"{prefix}: {keys_str}{suffix}")


def set_nested_val(data: dict, keys: list, value: Any) -> None:
    """Sets a value in a nested dictionary given a list of keys."""
    current = data
    for key in keys[:-1]:
        if key not in current or not isinstance(current[key], dict):
            current[key] = {}
        current = current[key]
    current[keys[-1]] = value


def split_array_elements(array_str: str) -> List[str]:
    """Splits a TOML array string by commas, respecting double and single quotes."""
    elements = []
    current_element = []
    in_double_quote = False
    in_single_quote = False
    
    for char in array_str:
        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            current_element.append(char)
        elif char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            current_element.append(char)
        elif char == ',' and not in_double_quote and not in_single_quote:
            elements.append("".join(current_element).strip())
            current_element = []
        else:
            current_element.append(char)
            
    if current_element:
        elements.append("".join(current_element).strip())
        
    return elements


def parse_toml_value(val_str: str) -> Any:
    """Parses a raw TOML value string into its appropriate Python type."""
    val_str = val_str.strip()
    if not val_str:
        return ""

    # 1. Parse array
    if val_str.startswith('[') and val_str.endswith(']'):
        content = val_str[1:-1].strip()
        if not content:
            return []
        elements = split_array_elements(content)
        return [parse_toml_value(elem) for elem in elements]

    # 2. Parse double-quoted string
    if val_str.startswith('"') and val_str.endswith('"'):
        inner = val_str[1:-1]
        inner = inner.replace('\\"', '"')
        inner = inner.replace('\\n', '\n')
        inner = inner.replace('\\t', '\t')
        return inner.replace('\\\\', '\\')

    # 3. Parse single-quoted string
    if val_str.startswith("'") and val_str.endswith("'"):
        inner = val_str[1:-1]
        inner = inner.replace("\\'", "'")
        inner = inner.replace('\\n', '\n')
        inner = inner.replace('\\t', '\t')
        return inner.replace('\\\\', '\\')

    # 4. Parse boolean
    val_lower = val_str.lower()
    if val_lower == "true":
        return True
    if val_lower == "false":
        return False

    # 5. Parse integer
    try:
        if re.match(r'^[-+]?\d+$', val_str):
            return int(val_str)
    except ValueError:
        pass

    # 6. Parse float
    try:
        if re.match(r'^[-+]?\d+\.\d+$', val_str):
            return float(val_str)
    except ValueError:
        pass

    return val_str


def _parse_toml_fallback(content: str) -> dict:
    """Hand-rolled TOML parser for older Python versions (< 3.11)."""
    data = {}
    current_table_keys = []
    
    in_double_quote = False
    in_single_quote = False
    open_brackets = 0
    open_braces = 0
    
    def _is_clean() -> bool:
        return not in_double_quote and not in_single_quote and open_brackets == 0 and open_braces == 0
    
    buffer = []
    
    for raw_line in content.splitlines():
        clean_chars = []
        for char in raw_line:
            if char == '"' and not in_single_quote:
                in_double_quote = not in_double_quote
                clean_chars.append(char)
            elif char == "'" and not in_double_quote:
                in_single_quote = not in_single_quote
                clean_chars.append(char)
            elif char == '#' and not in_double_quote and not in_single_quote:
                break
            else:
                if not in_double_quote and not in_single_quote:
                    if char == '[':
                        open_brackets += 1
                    elif char == ']':
                        open_brackets -= 1
                    elif char == '{':
                        open_braces += 1
                    elif char == '}':
                        open_braces -= 1
                clean_chars.append(char)
                
        line_stripped = "".join(clean_chars).strip()
        if not line_stripped and not buffer:
            continue
            
        buffer.append(line_stripped)
        
        if _is_clean():
            logical_line = " ".join(buffer).strip()
            buffer = []
            
            if not logical_line:
                continue
                
            if logical_line.startswith('[') and logical_line.endswith(']'):
                table_name = logical_line[1:-1].strip()
                current_table_keys = [k.strip() for k in table_name.split('.')]
                current = data
                for key in current_table_keys:
                    if key not in current or not isinstance(current[key], dict):
                        current[key] = {}
                    current = current[key]
            elif '=' in logical_line:
                key_part, val_part = logical_line.split('=', 1)
                key = key_part.strip()
                val = parse_toml_value(val_part.strip())
                
                if not current_table_keys:
                    data[key] = val
                else:
                    set_nested_val(data, current_table_keys + [key], val)
                    
    if not _is_clean():
        raise ValueError("Toml format error, unclosed brackets or quotes.")

    return data


def parse_toml(content: str) -> dict:
    """Parses a TOML string into a dictionary.

    Uses the native `tomllib` on Python 3.11+, and falls back to a custom,
    fully compatible fallback parser on older Python versions.
    """
    if HAS_TOMLLIB and tomllib is not None:
        return tomllib.loads(content)
    return _parse_toml_fallback(content)


def merge_toml(dict_a: dict, dict_b: dict) -> dict:
    """Recursively merges dictionary dict_b into dict_a, returning a new dictionary."""
    result = {}
    for key, value in dict_a.items():
        if key in dict_b:
            if isinstance(value, dict) and isinstance(dict_b[key], dict):
                result[key] = merge_toml(value, dict_b[key])
            else:
                result[key] = dict_b[key]
        else:
            result[key] = value
    for key, value in dict_b.items():
        if key not in result:
            result[key] = value
    return result


def dump_toml(data: dict) -> str:
    """Serializes a dictionary of basic package/workspace settings back to TOML format."""
    lines = []
    
    # 1. First, serialize any top-level key-values (outside tables)
    for k, v in data.items():
        if not isinstance(v, dict):
            if isinstance(v, bool):
                lines.append(f"{k} = {str(v).lower()}")
            elif isinstance(v, int):
                lines.append(f"{k} = {v}")
            elif isinstance(v, list):
                items = [f'"{str(i)}"' for i in v]
                lines.append(f"{k} = [{', '.join(items)}]")
            elif v is None:
                continue
            else:
                escaped_val = str(v).replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'{k} = "{escaped_val}"')

    # 2. Then, serialize nested tables (like [package] or [workspace])
    for table_name, table_dict in data.items():
        if isinstance(table_dict, dict):
            if lines:
                lines.append("")  # Empty line separator
            lines.append(f"[{table_name}]")
            for k, v in table_dict.items():
                if isinstance(v, dict):
                    # For nested tables (e.g. [packages.enable] or [render.envsubst])
                    # We can support one level of nested sub-table simply
                    sub_lines = []
                    sub_lines.append(f"[{table_name}.{k}]")
                    for sk, sv in v.items():
                        if isinstance(sv, bool):
                            sub_lines.append(f"{sk} = {str(sv).lower()}")
                        elif isinstance(sv, int):
                            sub_lines.append(f"{sk} = {sv}")
                        elif isinstance(sv, list):
                            items = [f'"{str(si)}"' for si in sv]
                            sub_lines.append(f"{sk} = [{', '.join(items)}]")
                        elif sv is None:
                            continue
                        else:
                            escaped_val = str(sv).replace("\\", "\\\\").replace('"', '\\"')
                            sub_lines.append(f'{sk} = "{escaped_val}"')
                    lines.append("\n".join(sub_lines))
                else:
                    if isinstance(v, bool):
                        lines.append(f"{k} = {str(v).lower()}")
                    elif isinstance(v, int):
                        lines.append(f"{k} = {v}")
                    elif isinstance(v, list):
                        items = [f'"{str(i)}"' for i in v]
                        lines.append(f"{k} = [{', '.join(items)}]")
                    elif v is None:
                        continue
                    else:
                        escaped_val = str(v).replace("\\", "\\\\").replace('"', '\\"')
                        lines.append(f'{k} = "{escaped_val}"')

    return "\n".join(lines) + "\n"
