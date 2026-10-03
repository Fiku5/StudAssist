import pytest
from pathlib import Path
from PIL import Image

from publisher.github_sync import GitHubPublisher, slugify, mask_credentials
from ai.schemas import (
    ActionItem,
    ActionItemsDigest,
    LectureSection,
    IllustratedLongread,
    LectureAnalysisResult,
)


def test_slugify():
    assert slugify("Архитектура ПО") == "Архитектура_ПО"
    assert slugify("Dependency Injection & IoC!") == "Dependency_Injection_IoC"
    assert slugify("   Лекция 1: Тест   ") == "Лекция_1_Тест"


def test_parse_repo_url():
    pub = GitHubPublisher(repo_url="https://github.com/my-university/cs-lectures.git")
    assert pub.owner == "my-university"
    assert pub.repo == "cs-lectures"

    pub_ssh = GitHubPublisher(repo_url="git@github.com:pavel/student-bot.git")
    assert pub_ssh.owner == "pavel"
    assert pub_ssh.repo == "student-bot"


def test_github_publisher_local_sync(tmp_path):
    mock_repo_dir = tmp_path / "mock_repo"
    publisher = GitHubPublisher(local_repo_dir=mock_repo_dir)
    publisher.simulation_mode = True

    # 1. Create fake slide
    slide_file = tmp_path / "slide_001_00m00s.png"
    img = Image.new("RGB", (100, 100), color=(10, 20, 30))
    img.save(slide_file)

    # 2. Prepare analysis result
    digest = ActionItemsDigest(
        subject="Базы Данных",
        topic="Нормализация и индексы",
        executive_summary="Разобрали 1NF-3NF и B-Tree индексы.",
        action_items=[
            ActionItem(category="lab", title="Лаба №2", description="Построить ERD", deadline="20 окт")
        ],
        key_technologies_and_terms=["B-Tree", "3NF"],
    )
    longread = IllustratedLongread(
        title="БД: Нормализация",
        subject="Базы Данных",
        topic="Нормализация и индексы",
        overview="Обзор нормальных форм",
        sections=[
            LectureSection(
                heading="Первая нормальная форма",
                timecode="05m00s",
                content_markdown="Атомарность значений.",
                referenced_slide_filenames=["slide_001_00m00s.png"],
            )
        ],
    )
    res = LectureAnalysisResult(digest=digest, longread=longread)

    # 3. Publish
    url = publisher.publish_lecture(
        analysis_result=res,
        slide_files=[slide_file],
        lecture_date="2026-09-15",
    )

    assert "Базы_Данных" in url
    assert "2026-09-15_Нормализация_и_индексы" in url

    # 4. Check files in mock repo in Lecture Bundle format
    bundle_dir = mock_repo_dir / "Базы_Данных" / "2026-09-15_Нормализация_и_индексы"
    assert bundle_dir.exists()

    md_path = bundle_dir / "README.md"
    assert md_path.exists()
    assert "Первая нормальная форма" in md_path.read_text(encoding="utf-8")

    asset_path = bundle_dir / "slides" / "slide_001_00m00s.png"
    assert asset_path.exists()

    # 5. Check Subject-level README.md
    subj_readme = mock_repo_dir / "Базы_Данных" / "README.md"
    assert subj_readme.exists()
    assert "Нормализация и индексы" in subj_readme.read_text(encoding="utf-8")

    # 6. Check Root README.md
    readme_path = mock_repo_dir / "README.md"
    assert readme_path.exists()
    readme_content = readme_path.read_text(encoding="utf-8")
    assert "| 2026-09-15 | **Базы Данных** |" in readme_content
    assert "Лаба №2 (20 окт)" in readme_content

    # 7. Re-publish same lecture to verify row replacement and no duplicate table rows
    digest_v2 = digest.model_copy(deep=True)
    digest_v2.action_items = [
        ActionItem(category="lab", title="Лаба №2 (Срок продлен)", description="Построить ERD", deadline="25 окт")
    ]
    res_v2 = LectureAnalysisResult(digest=digest_v2, longread=longread)
    publisher.publish_lecture(
        analysis_result=res_v2,
        slide_files=[slide_file],
        lecture_date="2026-09-15",
    )
    readme_content_v2 = readme_path.read_text(encoding="utf-8")
    assert readme_content_v2.count("2026-09-15_Нормализация_и_индексы") == 1
    assert "25 окт" in readme_content_v2
    assert "20 окт" not in readme_content_v2
    subj_readme_v2 = subj_readme.read_text(encoding="utf-8")
    assert subj_readme_v2.count("2026-09-15_Нормализация_и_индексы") == 1
    assert "25 окт" in subj_readme_v2
    assert "20 окт" not in subj_readme_v2


def test_create_or_update_file_retries_on_409(monkeypatch):
    """Verifies that _create_or_update_file catches 409 Conflict and retries with backoff."""
    from unittest.mock import MagicMock
    import httpx

    pub = GitHubPublisher(repo_url="https://github.com/test/repo", token="fake_token")

    mock_client = MagicMock()
    mock_get = MagicMock()
    mock_get.status_code = 200
    mock_get.json.return_value = {"sha": "sha_v1"}
    mock_client.get.return_value = mock_get

    # 1st put: 409 Conflict, 2nd put: 200 OK
    resp_409 = MagicMock()
    resp_409.status_code = 409
    resp_200 = MagicMock()
    resp_200.status_code = 200

    mock_client.put.side_effect = [resp_409, resp_200]

    # Speed up sleep during tests
    monkeypatch.setattr("time.sleep", lambda s: None)

    pub._create_or_update_file(
        client=mock_client,
        path="slides/slide_001.png",
        content_bytes=b"fake_image_data",
        commit_msg="Add slide",
        sha=None,
        max_retries=3,
    )

    assert mock_client.put.call_count == 2


def test_trees_api_publish_single_atomic_commit(monkeypatch, tmp_path):
    """Verifies that _publish_remote_trees_api creates blobs, tree, commit, and patches ref atomically."""
    from unittest.mock import MagicMock

    pub = GitHubPublisher(repo_url="https://github.com/test/repo", token="fake_token")
    pub.simulation_mode = False

    slide_file = tmp_path / "slide_001.png"
    slide_file.write_bytes(b"test_slide_bytes")

    digest = ActionItemsDigest(
        subject="Сети",
        topic="ARP",
        executive_summary="Разбор ARP",
        action_items=[],
        key_technologies_and_terms=["ARP"],
    )
    longread = IllustratedLongread(
        title="ARP",
        subject="Сети",
        topic="ARP",
        overview="Обзор",
        sections=[],
    )
    res = LectureAnalysisResult(digest=digest, longread=longread)

    mock_client = MagicMock()

    # GET responses:
    # 1. ref/heads/main -> 200 with object.sha
    # 2. commits/{sha} -> 200 with tree.sha
    # 3. contents/Сети/README.md -> 404 (not exists yet)
    # 4. contents/README.md -> 200 (root readme exists)
    def mock_get(url, **kwargs):
        resp = MagicMock()
        if "git/ref/heads" in url:
            resp.status_code = 200
            resp.json.return_value = {"object": {"sha": "commit_sha_123"}}
        elif "git/commits" in url:
            resp.status_code = 200
            resp.json.return_value = {"tree": {"sha": "tree_sha_base"}}
        elif "README.md" in url:
            resp.status_code = 200
            import base64
            resp.json.return_value = {"content": base64.b64encode(b"# Root\n").decode("ascii")}
        else:
            resp.status_code = 404
        return resp

    mock_client.get.side_effect = mock_get

    # POST responses:
    # 1. git/blobs -> 201 with sha
    # 2. git/trees -> 201 with sha
    # 3. git/commits -> 201 with sha
    blob_counter = 0
    def mock_post(url, **kwargs):
        nonlocal blob_counter
        resp = MagicMock()
        resp.status_code = 201
        if "git/blobs" in url:
            blob_counter += 1
            resp.json.return_value = {"sha": f"blob_sha_{blob_counter}"}
        elif "git/trees" in url:
            resp.json.return_value = {"sha": "new_tree_sha_456"}
        elif "git/commits" in url:
            resp.json.return_value = {"sha": "new_commit_sha_789"}
        return resp

    mock_client.post.side_effect = mock_post

    # PATCH response for ref update
    mock_patch = MagicMock()
    mock_patch.status_code = 200
    mock_client.patch.return_value = mock_patch

    success = pub._publish_remote_trees_api(
        client=mock_client,
        bundle_rel_dir="Сети/2026-09-16_ARP",
        note_rel_path="Сети/2026-09-16_ARP/README.md",
        slides_rel_dir="Сети/2026-09-16_ARP/slides",
        attachments_rel_dir="Сети/2026-09-16_ARP/attachments",
        subject_slug="Сети",
        md_content="# ARP Note",
        slide_files=[slide_file],
        attachment_files=[],
        analysis_result=res,
        date_str="2026-09-16",
    )

    assert success is True
    # Verify PATCH ref was called with new commit SHA
    mock_client.patch.assert_called_once()
    patch_call = mock_client.patch.call_args
    assert "git/refs/heads/main" in patch_call[0][0]
    assert patch_call[1]["json"]["sha"] == "new_commit_sha_789"


def test_publish_remote_api_falls_back_to_git_cli(monkeypatch, tmp_path):
    from unittest.mock import MagicMock
    import httpx

    pub = GitHubPublisher(
        repo_url="https://github.com/my-uni/lectures.git",
        token="test-token",
        branch="main",
    )
    pub.simulation_mode = False

    # Simulate httpx.Client throwing ConnectTimeout (e.g. SSL handshake timeout)
    def mock_client_init(*args, **kwargs):
        mock_ctx = MagicMock()
        mock_ctx.__enter__.side_effect = httpx.ConnectTimeout("_ssl.c:983: The handshake operation timed out")
        return mock_ctx

    monkeypatch.setattr(httpx, "Client", mock_client_init)

    # Mock _publish_git_cli
    mock_git_cli = MagicMock(return_value="https://github.com/my-uni/lectures/tree/main/test_bundle")
    monkeypatch.setattr(pub, "_publish_git_cli", mock_git_cli)

    res = pub._publish_remote_api(
        bundle_rel_dir="test_bundle",
        note_rel_path="test_bundle/README.md",
        slides_rel_dir="test_bundle/slides",
        attachments_rel_dir="test_bundle/attachments",
        subject_slug="seti",
        md_content="# Test",
        slide_files=[],
        attachment_files=[],
        analysis_result=MagicMock(),
        date_str="2026-09-15",
    )

    assert res == "https://github.com/my-uni/lectures/tree/main/test_bundle"
    mock_git_cli.assert_called_once()


def test_mask_credentials():
    # Mask embedded token in URL
    raw_url = "https://x-access-token:ghp_SuperSecret12345@github.com/owner/repo.git"
    masked = mask_credentials(raw_url)
    assert "ghp_SuperSecret12345" not in masked
    assert "https://x-access-token:***@github.com/owner/repo.git" == masked

    # Mask standalone token
    err_text = "fatal: could not read Password for 'https://ghp_SuperSecret12345@github.com': No such device"
    masked_err = mask_credentials(err_text, token="ghp_SuperSecret12345")
    assert "ghp_SuperSecret12345" not in masked_err
    assert "***" in masked_err

    # Empty text
    assert mask_credentials("") == ""


def test_git_auth_options_in_memory_only():
    import base64

    # With token
    pub = GitHubPublisher(repo_url="https://github.com/my-org/lectures.git", token="ghp_SecretTokenXYZ")
    assert pub._get_clean_repo_url() == "https://github.com/my-org/lectures.git"
    assert "ghp_SecretTokenXYZ" not in pub._get_clean_repo_url()

    auth_opts = pub._get_git_auth_options()
    assert len(auth_opts) == 2
    assert auth_opts[0] == "-c"
    assert auth_opts[1].startswith("http.extraheader=AUTHORIZATION: basic ")

    encoded_header = auth_opts[1].split("basic ")[1]
    decoded = base64.b64decode(encoded_header).decode("utf-8")
    assert decoded == "x-access-token:ghp_SecretTokenXYZ"

    # Without token
    pub_no_token = GitHubPublisher(repo_url="https://github.com/my-org/lectures.git", token=None)
    assert pub_no_token._get_git_auth_options() == []


def test_publish_git_cli_masks_error_on_push_failure(monkeypatch, tmp_path):
    from unittest.mock import MagicMock
    import subprocess

    pub = GitHubPublisher(repo_url="https://github.com/test-owner/test-repo.git", token="ghp_SensitivePushToken")
    pub.simulation_mode = False

    # Mock _publish_local
    monkeypatch.setattr(pub, "_publish_local", lambda **kwargs: None)

    # Mock repo_dir existence
    mock_repo_dir = tmp_path / "fake_repo"
    mock_repo_dir.mkdir()
    (mock_repo_dir / ".git").mkdir()
    monkeypatch.setattr("publisher.github_sync.BASE_DIR", tmp_path)

    # Mock subprocess.run
    def mock_subprocess_run(cmd, *args, **kwargs):
        res = MagicMock()
        # If git push, simulate failure with error containing sensitive token
        if "push" in cmd:
            res.returncode = 1
            res.stderr = "fatal: unable to access 'https://x-access-token:ghp_SensitivePushToken@github.com/test-owner/test-repo.git/': Failed"
            return res
        # If diff --cached, return non-zero to trigger commit and push
        if "diff" in cmd:
            res.returncode = 1
            return res
        res.returncode = 0
        res.stdout = ""
        res.stderr = ""
        return res

    monkeypatch.setattr(subprocess, "run", mock_subprocess_run)

    with pytest.raises(RuntimeError) as exc_info:
        pub._publish_git_cli(
            bundle_rel_dir="math/lecture_1",
            note_rel_path="math/lecture_1/README.md",
            subject_slug="math",
            md_content="# Math",
            slide_files=[],
            attachment_files=[],
            analysis_result=MagicMock(),
            date_str="2026-09-24",
        )

    error_msg = str(exc_info.value)
    assert "ghp_SensitivePushToken" not in error_msg
    assert "***" in error_msg


