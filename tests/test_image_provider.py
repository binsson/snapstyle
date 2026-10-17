import base64

import pytest

import server


class Response:
    def __init__(self, body=None, status=200, content=b"", headers=None):
        self.body = body
        self.status_code = status
        self.content = content
        self.headers = headers or {}

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise server.req_lib.HTTPError(response=self)


@pytest.fixture
def toapis(monkeypatch):
    monkeypatch.setattr(server, "IMAGE_API_PROVIDER", "toapis")
    monkeypatch.setattr(server, "API_BASE", "https://provider.example/v1")
    monkeypatch.setattr(server, "MODEL", "gpt-image-2.5-sunburst")
    monkeypatch.setattr(server, "IMAGE_RESOLUTION", "1K")
    monkeypatch.setattr(server.time, "sleep", lambda _: None)


def params(with_images=True):
    return {
        "prompt": "Preserve the garment and apply the reference style.",
        "input_images": [
            {"data": base64.b64encode(data).decode(), "mime": "image/jpeg"}
            for data in (b"product", b"style")
        ] if with_images else [],
        "output_size": "1024x1536",
        "art_direct": False,
    }


@pytest.mark.parametrize("poll_problem,guidance_source", [
    (None, None), ("connection", None), ("rate_limit", None),
    (None, "reference"), (None, "thumbnail"), (None, "failure"),
])
def test_toapis_reference_generation_reuses_task_and_downloads_without_key(toapis, monkeypatch, poll_problem, guidance_source):
    uploads, submissions, polls = [], [], []

    class Session:
        def post(self, url, **kwargs):
            if url.endswith("/uploads/images"):
                uploads.append(kwargs["files"]["file"][1].read())
                return Response({"success": True, "data": {"url": f"https://cdn.example/ref-{len(uploads)}.jpg"}})
            assert url.endswith("/images/generations")
            submissions.append(kwargs["json"])
            return Response({"id": "task-123", "status": "pending"})

        def get(self, url, **kwargs):
            assert url == "https://provider.example/v1/images/generations/task-123"
            polls.append(url)
            if len(polls) == 1:
                if poll_problem == "connection":
                    raise server.req_lib.ConnectionError("temporary polling failure")
                if poll_problem == "rate_limit":
                    return Response(status=429, headers={"Retry-After": "5"})
            return Response({"status": "completed", "result": {"data": [{"url": "https://cdn.example/result.png"}]}})

    def public_download(url, **kwargs):
        assert url == "https://cdn.example/result.png"
        assert "headers" not in kwargs and "auth" not in kwargs
        return Response(content=b"generated-image")

    monkeypatch.setattr(server, "SESSION", Session())
    monkeypatch.setattr(server.req_lib, "get", public_download)
    generation_params = params()
    guidance_requests = []
    if guidance_source:
        monkeypatch.setattr(server, "REWRITE_ENABLED", True)
        generation_params.update(
            art_direct=True,
            style_spec={"lighting": "legacy lighting", "styling": "invented garment"},
            shot_intent="worn_full_body",
            prev_brief="old garment interpretation",
        )
        if guidance_source == "thumbnail":
            generation_params["style_thumb"] = {"data": "style-thumbnail", "mime": "image/webp"}

        def rewrite(url, **kwargs):
            guidance_requests.append(kwargs["json"])
            if guidance_source == "failure":
                raise server.req_lib.ConnectionError("guidance unavailable")
            return Response({"choices": [{"message": {"content": "Soft daylight, open composition and a relaxed mood."}}]})

        monkeypatch.setattr(server.req_lib, "post", rewrite)

    image, brief = server._run_generation("test", generation_params)
    assert base64.b64decode(image) == b"generated-image"
    assert uploads == [b"product", b"style"]
    assert len(submissions) == 1, "Polling failures must not create a second billed task"
    assert submissions[0]["model"] == "gpt-image-2.5-sunburst"
    assert submissions[0]["size"] == "2:3"
    assert submissions[0]["resolution"] == "1k"
    assert submissions[0]["reference_images"] == ["https://cdn.example/ref-1.jpg", "https://cdn.example/ref-2.jpg"]
    if guidance_source:
        content = guidance_requests[0]["messages"][1]["content"]
        image_urls = [item["image_url"]["url"] for item in content if item["type"] == "image_url"]
        expected = ("data:image/webp;base64,style-thumbnail" if guidance_source == "thumbnail"
                    else "data:image/jpeg;base64," + base64.b64encode(b"style").decode())
        assert image_urls == [expected], "Only the style image may enter the guidance request"
        assert "legacy lighting" not in str(guidance_requests)
        assert "invented garment" not in str(guidance_requests)
        assert "old garment interpretation" not in str(guidance_requests)
        assert "old garment interpretation" not in submissions[0]["prompt"]
        assert brief and len(brief.split()) <= 40


def test_toapis_failed_task_is_not_resubmitted(toapis, monkeypatch):
    submissions = []

    class Session:
        def post(self, url, **kwargs):
            submissions.append(kwargs["json"])
            return Response({"id": "failed-task", "status": "queued"})

        def get(self, url, **kwargs):
            return Response({"status": "failed", "error": {"message": "provider failure"}})

    monkeypatch.setattr(server, "SESSION", Session())
    with pytest.raises(server.GenerationError):
        server._run_generation("test-failure", params(with_images=False))
    assert len(submissions) == 1
    assert "reference_images" not in submissions[0]


@pytest.mark.parametrize("image_count", [0, 1, 2])
@pytest.mark.parametrize("result_format", ["b64_json", "url"])
def test_native_provider_preserves_inputs_and_omits_resolution(monkeypatch, image_count, result_format):
    monkeypatch.setattr(server, "IMAGE_API_PROVIDER", "openai")
    monkeypatch.setattr(server, "API_BASE", "https://native.example/v1")
    monkeypatch.setattr(server, "MODEL", "gpt-image-2.5-sunburst")
    monkeypatch.setattr(server, "IMAGE_SIZE_ENABLED", False)
    monkeypatch.setattr(server, "IMAGE_RESOLUTION", "")
    monkeypatch.setattr(server.time, "sleep", lambda _: None)
    submissions, downloads = [], []
    generation_params = params()
    generation_params["input_images"] = generation_params["input_images"][:image_count]

    class Session:
        def post(self, url, **kwargs):
            submissions.append(url)
            data = kwargs["data"] if image_count else kwargs["json"]
            assert data["model"] == "gpt-image-2.5-sunburst"
            assert data["prompt"] == generation_params["prompt"]
            assert not {"size", "resolution", "input_fidelity"} & data.keys()
            if image_count:
                assert url.endswith("/images/edits")
                assert [name for name, _ in kwargs["files"]] == ["image[]" if image_count > 1 else "image"] * image_count
                assert [file[1].read() for _, file in kwargs["files"]] == [b"product", b"style"][:image_count]
            else:
                assert url.endswith("/images/generations")
                assert "files" not in kwargs
            item = ({"b64_json": base64.b64encode(b"generated-image").decode()}
                    if result_format == "b64_json" else {"url": "https://cdn.example/native.png"})
            return Response({"data": [item]})

    def download(url, **kwargs):
        downloads.append(url)
        assert "headers" not in kwargs and "auth" not in kwargs
        if len(downloads) == 1:
            raise server.req_lib.ConnectionError("temporary CDN failure")
        return Response(content=b"generated-image")

    monkeypatch.setattr(server, "SESSION", Session())
    monkeypatch.setattr(server.req_lib, "get", download)
    image, _ = server._run_generation("native-test", generation_params)
    assert base64.b64decode(image) == b"generated-image"
    assert len(submissions) == 1, "Downloading an existing result must not submit another billed generation"


def test_upstream_error_logging_does_not_expose_credentials(monkeypatch):
    monkeypatch.setattr(server, "API_KEY", "private-image-credential")
    monkeypatch.setattr(server, "REWRITE_API_KEY", "private-rewrite-credential")
    monkeypatch.setattr(server, "API_BASE", "https://private-image.example/v1")
    monkeypatch.setattr(server, "REWRITE_API_BASE", "https://private-rewrite.example/v1")
    response = Response(
        {"error": {"message": "Rejected private-image-credential private-rewrite-credential sk-extra-secret https://private-image.example/v1/images/edits private-rewrite.example"}},
        status=401, headers={"Content-Type": "application/json", "X-Request-Id": "request-123"},
    )
    details = server.image_upstream_error_details(response)
    assert "private-image-credential" not in details
    assert "private-rewrite-credential" not in details
    assert "sk-extra-secret" not in details
    assert "private-image.example" not in details
    assert "private-rewrite.example" not in details
    assert "request-123" in details


def test_upstream_error_redacts_credentials_before_truncating(monkeypatch):
    monkeypatch.setattr(server, "API_KEY", "private-credential-that-crosses-the-log-boundary")
    response = Response(
        {"error": {"message": "x" * 390 + server.API_KEY}},
        status=401,
        headers={"Content-Type": "application/json", "X-Request-Id": "x" * 110 + server.API_KEY},
    )
    assert "private-" not in server.image_upstream_error_details(response)
