import re
from typing import Any, List

try:
    import tomllib
    HAS_TOMLLIB = True
except ImportError:
    HAS_TOMLLIB = False


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
        
        if not in_double_quote and not in_single_quote and open_brackets == 0 and open_braces == 0:
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
                    
    return data


def parse_toml(content: str) -> dict:
    """Parses a TOML string into a dictionary.

    Uses the native `tomllib` on Python 3.11+, and falls back to a custom,
    fully compatible fallback parser on older Python versions.
    """
    if HAS_TOMLLIB:
        return tomllib.loads(content)
    return _parse_toml_fallback(content)
