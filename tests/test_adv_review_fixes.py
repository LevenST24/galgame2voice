"""
Tests for ADV review fixes:
A2: Outfit validation on ChatRequest (the web acting-menu paths are covered in
    test_p2b_lines_streaming.py).
"""

from galgame2voice.routers.chat import _validate_chat_request, ChatRequest


def test_chat_request_outfit_security_validation():
    """Validates that ChatRequest outfit parameter rejects path traversals, special chars, and excessive lengths."""
    # 1. Path traversal attacks
    req_traversal1 = ChatRequest(prompt="hello", outfit="../secret/costume")
    _validate_chat_request(req_traversal1)
    assert req_traversal1.outfit is None

    req_traversal2 = ChatRequest(prompt="hello", outfit="..\\secret")
    _validate_chat_request(req_traversal2)
    assert req_traversal2.outfit is None

    # 2. Slash and backslash
    req_slash = ChatRequest(prompt="hello", outfit="costume/sub")
    _validate_chat_request(req_slash)
    assert req_slash.outfit is None

    # 3. Excessive length (> 200 chars), single dot, and control characters
    for bad in ("a" * 201, ".", "a\nb"):
        req_bad = ChatRequest(prompt="hello", outfit=bad)
        _validate_chat_request(req_bad)
        assert req_bad.outfit is None

    # 4. Valid outfit identifiers (up to 200 chars, including spaces/dots and untrimmed spacing)
    for valid in ("summer_dress_2", "school uniform ", "a" * 200):
        req_valid = ChatRequest(prompt="hello", outfit=valid)
        _validate_chat_request(req_valid)
        assert req_valid.outfit == valid

    # 5. Empty or whitespace outfit
    req_empty = ChatRequest(prompt="hello", outfit="   ")
    _validate_chat_request(req_empty)
    assert req_empty.outfit is None
