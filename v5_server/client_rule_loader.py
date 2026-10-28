"""Load the small, verified client rule exports used by reward operations."""
from functools import lru_cache
import json
from pathlib import Path

from core import OperationError


@lru_cache(maxsize=None)
def load_rules(filename):
    try:
        value = json.loads((Path(__file__).parent / filename).read_text(encoding='utf-8'))
        if not isinstance(value, dict) or value.get('schema_version') != 1:
            raise ValueError('invalid rule schema')
        return value
    except (OSError, ValueError) as exc:
        raise OperationError(406, f'服务端规则配置缺失或无效: {filename}') from exc
