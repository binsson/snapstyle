import pytest

import server


def test_disabled_rewrite_generates_brief_without_calling_upstream(monkeypatch):
    monkeypatch.setattr(server, "REWRITE_ENABLED", False)

    def unexpected_request(*args, **kwargs):
        pytest.fail("Disabled rewrite must not contact the upstream provider")

    monkeypatch.setattr(server.req_lib, "post", unexpected_request)
    brief = server.art_direct_brief(
        {"lighting": "Soft window light.", "styling": "invented garment description"},
        "deployment-test",
    )
    assert "Soft window light." in brief
    assert "invented garment" not in brief
    with pytest.raises(ValueError, match="风格分析暂未配置"):
        server.analyze_style_image({"data": "unused"}, "deployment-test")


def test_long_guidance_falls_back_to_reference_without_legacy_details(monkeypatch):
    monkeypatch.setattr(server, "REWRITE_ENABLED", True)
    calls = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "Detailed staging. " * 100}}]}

    def rewrite(url, **kwargs):
        calls.append(kwargs["json"])
        return Response()

    monkeypatch.setattr(server.req_lib, "post", rewrite)
    brief = server.art_direct_brief(
        {"lighting": "legacy color grading", "styling": "invented garment description"},
        "long-brief-test",
        style_image_item={"data": "style-reference", "mime": "image/jpeg"},
    )
    assert len(calls) == 1
    assert len(brief.split()) <= 40
    assert "Detailed staging" not in brief
    assert "legacy" not in brief
    assert "invented garment" not in brief
