import base64
import datetime
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import httpx

from config import settings, BASE_DIR, get_local_now
from ai.schemas import LectureAnalysisResult

logger = logging.getLogger(__name__)


def slugify(text: str, max_length: int = 100) -> str:
    """Converts a string into a clean filesystem/URL slug, without cutting off words mid-word."""
    text = text.strip()
    # Replace spaces with underscores and remove problematic characters
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[-\s]+", "_", text)
    slug = text.strip("_")
    if len(slug) > max_length:
        truncated = slug[:max_length]
        last_sep = truncated.rfind("_")
        if last_sep > max_length // 2:
            slug = truncated[:last_sep]
        else:
            slug = truncated.rstrip("_")
    return slug or "lecture"


def mask_credentials(text: str, token: Optional[str] = None) -> str:
    """
    Masks credentials and tokens embedded in URLs or error strings to prevent secret leakage in logs.
    """
    if not text:
        return ""
    masked = re.sub(r"(https?://)([^:/@]+:)?([^/@]+)@", r"\1\2***@", str(text))
    if token and token in masked:
        masked = masked.replace(token, "***")
    return masked


class GitHubPublisher:
    """
    Manages publication of lecture notes, slides, and master README table
    to a GitHub repository in the Lecture Bundle format:
    {Subject}/{YYYY-MM-DD}_{Topic}/README.md
    └── slides/
        ├── slide_001.png
        └── ...
    """

    def __init__(
        self,
        repo_url: Optional[str] = None,
        token: Optional[str] = None,
        branch: Optional[str] = None,
        local_repo_dir: Optional[Path] = None,
    ):
        self.repo_url = repo_url or settings.github_repo_url
        self.token = token or settings.github_token
        self.branch = branch or settings.github_branch
        self.local_repo_dir = local_repo_dir or (BASE_DIR / "mock_repo")
        self.simulation_mode = settings.simulation_mode or not bool(self.token)

        # Parse owner and repo name from URL (e.g. https://github.com/owner/repo.git)
        self.owner, self.repo = self._parse_repo_url(self.repo_url)

    def _parse_repo_url(self, url: str) -> Tuple[str, str]:
        """Extracts (owner, repo) from git/https URL, correctly supporting dots in repo names."""
        if not url:
            return ("student-group", "university-lectures")
        clean_url = url.strip()
        if clean_url.endswith(".git"):
            clean_url = clean_url[:-4]
        clean_url = clean_url.rstrip("/")
        match = re.search(r"github\.com[/:]([^/]+)/([^/]+)$", clean_url)
        if match:
            return match.group(1), match.group(2)
        return ("student-group", "university-lectures")

    def publish_lecture(
        self,
        analysis_result: LectureAnalysisResult,
        slide_files: List[Path],
        attachment_files: Optional[List[Path]] = None,
        lecture_date: Optional[str] = None,
        subject_name: Optional[str] = None,
    ) -> str:
        """
        Publishes the lecture note and its slide assets in Lecture Bundle format:
        {subject_slug}/{date_str}_{topic_slug}/README.md
        Returns the public URL to view the published markdown note.
        """
        now = get_local_now()
        date_str = lecture_date or now.strftime("%Y-%m-%d")

        # Prioritize official subject_name from bot/schedule if provided
        subject = subject_name or analysis_result.longread.subject or "Общий"
        topic = analysis_result.longread.topic or "Лекция"

        subject_slug = slugify(subject, max_length=60)
        topic_slug = slugify(topic, max_length=100)

        # Lecture bundle directory structure: {Subject}/{Date}_{Topic}/
        bundle_rel_dir = f"{subject_slug}/{date_str}_{topic_slug}"
        note_rel_path = f"{bundle_rel_dir}/README.md"
        slides_rel_dir = f"{bundle_rel_dir}/slides"
        attachments_rel_dir = f"{bundle_rel_dir}/attachments"

        # Generate markdown content with isolated relative slides path
        md_content = analysis_result.longread.to_markdown_document(assets_relative_path="slides")

        if self.simulation_mode:
            logger.info("GitHubPublisher running in LOCAL/SIMULATION mode.")
            return self._publish_local(
                bundle_rel_dir=bundle_rel_dir,
                note_rel_path=note_rel_path,
                subject_slug=subject_slug,
                md_content=md_content,
                slide_files=slide_files,
                attachment_files=attachment_files or [],
                analysis_result=analysis_result,
                date_str=date_str,
            )

        return self._publish_remote_api(
            bundle_rel_dir=bundle_rel_dir,
            note_rel_path=note_rel_path,
            slides_rel_dir=slides_rel_dir,
            attachments_rel_dir=attachments_rel_dir,
            subject_slug=subject_slug,
            md_content=md_content,
            slide_files=slide_files,
            attachment_files=attachment_files or [],
            analysis_result=analysis_result,
            date_str=date_str,
        )

    def _publish_local(
        self,
        bundle_rel_dir: str,
        note_rel_path: str,
        subject_slug: str,
        md_content: str,
        slide_files: List[Path],
        attachment_files: List[Path],
        analysis_result: LectureAnalysisResult,
        date_str: str,
        target_dir: Optional[Path] = None,
    ) -> str:
        """Saves content directly to local mock_repo or target git repository directory in bundle format."""
        repo_root = target_dir or self.local_repo_dir
        repo_root.mkdir(parents=True, exist_ok=True)
        bundle_dir = repo_root / bundle_rel_dir
        slides_dir = bundle_dir / "slides"
        attachments_dir = bundle_dir / "attachments"

        bundle_dir.mkdir(parents=True, exist_ok=True)
        slides_dir.mkdir(parents=True, exist_ok=True)

        # 1. Save Markdown note as bundle README.md
        note_full_path = repo_root / note_rel_path
        note_full_path.parent.mkdir(parents=True, exist_ok=True)
        note_full_path.write_text(md_content, encoding="utf-8")

        # 2. Copy slide assets into bundle's slides/
        for sf in slide_files:
            if sf.exists():
                dest = slides_dir / sf.name
                dest.write_bytes(sf.read_bytes())

        # 3. Copy teacher attachments if present
        if attachment_files:
            attachments_dir.mkdir(parents=True, exist_ok=True)
            for af in attachment_files:
                if af.exists():
                    dest = attachments_dir / af.name
                    dest.write_bytes(af.read_bytes())

        # 4. Update Subject-level README.md index
        subject_dir = repo_root / subject_slug
        subject_dir.mkdir(parents=True, exist_ok=True)
        subject_readme_path = subject_dir / "README.md"
        current_subj_readme = subject_readme_path.read_text(encoding="utf-8") if subject_readme_path.exists() else ""
        updated_subj_readme = self._update_subject_readme(
            current_readme=current_subj_readme,
            analysis_result=analysis_result,
            lecture_folder_name=bundle_dir.name,
            date_str=date_str,
        )
        subject_readme_path.write_text(updated_subj_readme, encoding="utf-8")

        # 5. Update Root README.md
        root_readme_path = repo_root / "README.md"
        current_root_readme = root_readme_path.read_text(encoding="utf-8") if root_readme_path.exists() else ""
        updated_root_readme = self._update_root_readme_index(
            current_readme=current_root_readme,
            analysis_result=analysis_result,
            bundle_rel_path=bundle_rel_dir,
            date_str=date_str,
        )
        root_readme_path.write_text(updated_root_readme, encoding="utf-8")

        mock_url = f"https://github.com/{self.owner}/{self.repo}/tree/{self.branch}/{bundle_rel_dir}"
        logger.info(f"Published to repo root: {bundle_dir.resolve()}")
        return mock_url

    def _get_clean_repo_url(self) -> str:
        """Returns standard HTTPS repository URL without any embedded credentials."""
        if self.owner and self.repo:
            return f"https://github.com/{self.owner}/{self.repo}.git"
        clean = (self.repo_url or "").strip()
        return mask_credentials(clean, self.token) if "@" in clean else clean

    def _get_git_auth_options(self) -> List[str]:
        """
        Builds Git CLI authentication options using http.extraheader.
        Keeps credentials strictly in-memory during command execution,
        preventing tokens from being persisted into .git/config on disk.
        """
        if not self.token:
            return []
        auth_bytes = f"x-access-token:{self.token}".encode("utf-8")
        auth_b64 = base64.b64encode(auth_bytes).decode("utf-8")
        return ["-c", f"http.extraheader=AUTHORIZATION: basic {auth_b64}"]

    def _publish_git_cli(
        self,
        bundle_rel_dir: str,
        note_rel_path: str,
        subject_slug: str,
        md_content: str,
        slide_files: List[Path],
        attachment_files: List[Path],
        analysis_result: LectureAnalysisResult,
        date_str: str,
    ) -> str:
        """Publishes lecture bundle using local git CLI and pushes directly to github.com."""
        repo_dir = BASE_DIR / "temp" / "github_sync_repo"
        repo_dir.parent.mkdir(parents=True, exist_ok=True)

        clean_url = self._get_clean_repo_url()
        auth_opts = self._get_git_auth_options()

        is_cloned = (repo_dir / ".git").is_dir()
        if not is_cloned:
            if repo_dir.exists():
                shutil.rmtree(repo_dir, ignore_errors=True)
            logger.info(f"Cloning GitHub repo via git CLI into {repo_dir}...")
            clone_cmd = ["git"] + auth_opts + ["clone", "--depth", "1"]
            if self.branch:
                clone_cmd.extend(["-b", self.branch])
            clone_cmd.extend([clean_url, str(repo_dir)])
            res = subprocess.run(clone_cmd, capture_output=True, text=True)
            if res.returncode != 0:
                err_msg = mask_credentials(res.stderr, self.token)
                logger.warning(f"Git clone with branch {self.branch} failed: {err_msg}. Retrying clone without -b...")
                res2 = subprocess.run(["git"] + auth_opts + ["clone", "--depth", "1", clean_url, str(repo_dir)], capture_output=True, text=True)
                if res2.returncode != 0:
                    raise RuntimeError(f"git clone failed: {mask_credentials(res2.stderr, self.token)}")
        else:
            logger.info("Updating existing git CLI repo clone...")
            # Ensure .git/config remote url is clean without embedded plaintext tokens
            subprocess.run(["git", "-C", str(repo_dir), "remote", "set-url", "origin", clean_url], check=False, capture_output=True)
            subprocess.run(["git", "-C", str(repo_dir)] + auth_opts + ["fetch", "--depth", "1", "origin", self.branch], check=False, capture_output=True)
            subprocess.run(["git", "-C", str(repo_dir), "checkout", self.branch], check=False, capture_output=True)
            subprocess.run(["git", "-C", str(repo_dir), "reset", "--hard", f"origin/{self.branch}"], check=False, capture_output=True)

        # Configure local git identity if needed
        subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "StudentAssistant Bot"], check=False, capture_output=True)
        subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "bot@studentassistant.local"], check=False, capture_output=True)

        # Render files into repo_dir
        self._publish_local(
            bundle_rel_dir=bundle_rel_dir,
            note_rel_path=note_rel_path,
            subject_slug=subject_slug,
            md_content=md_content,
            slide_files=slide_files,
            attachment_files=attachment_files,
            analysis_result=analysis_result,
            date_str=date_str,
            target_dir=repo_dir,
        )

        # Stage and commit
        subprocess.run(["git", "-C", str(repo_dir), "add", "-A"], check=True, capture_output=True)
        commit_msg = f"docs(lecture): {analysis_result.longread.subject} - {analysis_result.longread.topic} ({date_str})"

        diff_res = subprocess.run(["git", "-C", str(repo_dir), "diff", "--cached", "--quiet"])
        if diff_res.returncode != 0:
            subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", commit_msg], check=True, capture_output=True)
            logger.info(f"Pushing lecture bundle to GitHub via git CLI (branch {self.branch})...")
            push_res = subprocess.run(["git", "-C", str(repo_dir)] + auth_opts + ["push", "origin", self.branch], capture_output=True, text=True)
            if push_res.returncode != 0:
                raise RuntimeError(f"git push failed: {mask_credentials(push_res.stderr, self.token)}")
        else:
            logger.info("No changes to commit via git CLI.")

        published_url = f"https://github.com/{self.owner}/{self.repo}/tree/{self.branch}/{bundle_rel_dir}"
        logger.info(f"Published successfully via Git CLI: {published_url}")
        return published_url

    def _publish_remote_api(
        self,
        bundle_rel_dir: str,
        note_rel_path: str,
        slides_rel_dir: str,
        attachments_rel_dir: str,
        subject_slug: str,
        md_content: str,
        slide_files: List[Path],
        attachment_files: List[Path],
        analysis_result: LectureAnalysisResult,
        date_str: str,
    ) -> str:
        """Pushes files in bundle format using GitHub REST API with Git CLI fallback."""
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        client_kwargs = {
            "headers": headers,
            "timeout": httpx.Timeout(60.0, connect=15.0),
        }
        proxy = getattr(settings, "proxy", None) or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or os.getenv("ALL_PROXY")
        if proxy:
            client_kwargs["proxy"] = proxy

        try:
            with httpx.Client(**client_kwargs) as client:
                # 1. Attempt atomic single-commit upload via Git Trees API
                try:
                    tree_success = self._publish_remote_trees_api(
                        client=client,
                        bundle_rel_dir=bundle_rel_dir,
                        note_rel_path=note_rel_path,
                        slides_rel_dir=slides_rel_dir,
                        attachments_rel_dir=attachments_rel_dir,
                        subject_slug=subject_slug,
                        md_content=md_content,
                        slide_files=slide_files,
                        attachment_files=attachment_files,
                        analysis_result=analysis_result,
                        date_str=date_str,
                    )
                    if tree_success:
                        return f"https://github.com/{self.owner}/{self.repo}/tree/{self.branch}/{bundle_rel_dir}"
                except Exception as e:
                    logger.warning(
                        f"Git Trees API publish encountered an error ({e}); falling back to sequential Contents API...",
                        exc_info=True,
                    )

                # 2. Fallback to Contents API (with 409 retry protection)
                self._publish_remote_contents_api(
                    client=client,
                    bundle_rel_dir=bundle_rel_dir,
                    note_rel_path=note_rel_path,
                    slides_rel_dir=slides_rel_dir,
                    attachments_rel_dir=attachments_rel_dir,
                    subject_slug=subject_slug,
                    md_content=md_content,
                    slide_files=slide_files,
                    attachment_files=attachment_files,
                    analysis_result=analysis_result,
                    date_str=date_str,
                )

            return f"https://github.com/{self.owner}/{self.repo}/tree/{self.branch}/{bundle_rel_dir}"
        except Exception as api_err:
            masked_api = mask_credentials(str(api_err), self.token)
            logger.warning(
                f"GitHub REST API publish encountered an error ({masked_api}); falling back to local Git CLI...",
                exc_info=True,
            )
            try:
                return self._publish_git_cli(
                    bundle_rel_dir=bundle_rel_dir,
                    note_rel_path=note_rel_path,
                    subject_slug=subject_slug,
                    md_content=md_content,
                    slide_files=slide_files,
                    attachment_files=attachment_files,
                    analysis_result=analysis_result,
                    date_str=date_str,
                )
            except Exception as git_err:
                masked_git = mask_credentials(str(git_err), self.token)
                logger.error(f"Git CLI fallback publish also failed: {masked_git}", exc_info=True)
                raise api_err

    def _create_blob(self, client: httpx.Client, content_bytes: bytes, max_retries: int = 3) -> str:
        """Creates a git blob via GitHub API and returns its SHA."""
        url = f"https://api.github.com/repos/{self.owner}/{self.repo}/git/blobs"
        payload = {
            "content": base64.b64encode(content_bytes).decode("ascii"),
            "encoding": "base64",
        }
        for attempt in range(max_retries):
            resp = client.post(url, json=payload)
            if resp.status_code in (409, 429, 500, 502, 503, 504) and attempt < max_retries - 1:
                wait_sec = 1.5 * (attempt + 1)
                logger.warning(
                    f"Blob creation returned {resp.status_code}. Retrying in {wait_sec}s (attempt {attempt + 1}/{max_retries})..."
                )
                time.sleep(wait_sec)
                continue
            resp.raise_for_status()
            return resp.json()["sha"]
        raise RuntimeError(f"Failed to create blob after {max_retries} attempts")

    def _publish_remote_trees_api(
        self,
        client: httpx.Client,
        bundle_rel_dir: str,
        note_rel_path: str,
        slides_rel_dir: str,
        attachments_rel_dir: str,
        subject_slug: str,
        md_content: str,
        slide_files: List[Path],
        attachment_files: List[Path],
        analysis_result: LectureAnalysisResult,
        date_str: str,
    ) -> bool:
        """
        Publishes the lecture atomically in a SINGLE Git commit using GitHub Git Data (Trees) API:
        1. Reads current READMEs to calculate updated index tables.
        2. Uploads all files as raw blobs (detached from branch, zero branch lock contention).
        3. Creates a single Git tree combining base tree + new lecture bundle files.
        4. Creates a single commit.
        5. Atomically updates branch ref.
        Returns True on success, False if repository requires Contents API fallback.
        """
        ref_url = f"https://api.github.com/repos/{self.owner}/{self.repo}/git/ref/heads/{self.branch}"
        r_ref = client.get(ref_url)
        if r_ref.status_code == 404:
            logger.warning(f"Branch {self.branch} not found via Git ref API; falling back to Contents API.")
            return False
        r_ref.raise_for_status()
        parent_commit_sha = r_ref.json()["object"]["sha"]

        commit_url = f"https://api.github.com/repos/{self.owner}/{self.repo}/git/commits/{parent_commit_sha}"
        r_commit = client.get(commit_url)
        r_commit.raise_for_status()
        base_tree_sha = r_commit.json()["tree"]["sha"]

        # 1. Subject README
        subj_readme_path = f"{subject_slug}/README.md"
        r_subj = client.get(
            f"https://api.github.com/repos/{self.owner}/{self.repo}/contents/{subj_readme_path}",
            params={"ref": self.branch},
        )
        current_subj_readme = ""
        if r_subj.status_code == 200:
            current_subj_readme = base64.b64decode(r_subj.json().get("content", "")).decode("utf-8")

        lecture_folder = bundle_rel_dir.split("/")[-1]
        updated_subj = self._update_subject_readme(
            current_subj_readme, analysis_result, lecture_folder, date_str
        )

        # 2. Root README
        root_readme_url = f"https://api.github.com/repos/{self.owner}/{self.repo}/contents/README.md"
        r_root = client.get(root_readme_url, params={"ref": self.branch})
        current_root_readme = ""
        if r_root.status_code == 200:
            current_root_readme = base64.b64decode(r_root.json().get("content", "")).decode("utf-8")

        updated_root = self._update_root_readme_index(
            current_root_readme, analysis_result, bundle_rel_dir, date_str
        )

        # 3. Create blobs and prepare tree entries
        tree_entries: List[Dict[str, Any]] = []
        blob_cache: Dict[int, str] = {}

        def get_or_create_blob(content_bytes: bytes) -> str:
            h = hash(content_bytes)
            if h not in blob_cache:
                blob_cache[h] = self._create_blob(client, content_bytes)
            return blob_cache[h]

        # Slides
        for sf in slide_files:
            if sf.exists():
                sha = get_or_create_blob(sf.read_bytes())
                tree_entries.append({
                    "path": f"{slides_rel_dir}/{sf.name}",
                    "mode": "100644",
                    "type": "blob",
                    "sha": sha,
                })

        # Attachments
        for af in attachment_files:
            if af.exists():
                sha = get_or_create_blob(af.read_bytes())
                tree_entries.append({
                    "path": f"{attachments_rel_dir}/{af.name}",
                    "mode": "100644",
                    "type": "blob",
                    "sha": sha,
                })

        # Lecture Note README.md
        note_sha = get_or_create_blob(md_content.encode("utf-8"))
        tree_entries.append({
            "path": note_rel_path,
            "mode": "100644",
            "type": "blob",
            "sha": note_sha,
        })

        # Subject README.md
        subj_sha = get_or_create_blob(updated_subj.encode("utf-8"))
        tree_entries.append({
            "path": subj_readme_path,
            "mode": "100644",
            "type": "blob",
            "sha": subj_sha,
        })

        # Root README.md
        root_sha = get_or_create_blob(updated_root.encode("utf-8"))
        tree_entries.append({
            "path": "README.md",
            "mode": "100644",
            "type": "blob",
            "sha": root_sha,
        })

        # 4. Create Tree
        trees_url = f"https://api.github.com/repos/{self.owner}/{self.repo}/git/trees"
        tree_payload = {
            "base_tree": base_tree_sha,
            "tree": tree_entries,
        }
        r_tree = client.post(trees_url, json=tree_payload)
        r_tree.raise_for_status()
        new_tree_sha = r_tree.json()["sha"]

        # 5. Create Commit
        commits_url = f"https://api.github.com/repos/{self.owner}/{self.repo}/git/commits"
        subject_name = analysis_result.longread.subject or "Онлайн-пара"
        topic_name = analysis_result.longread.topic or "Лекция"
        commit_payload = {
            "message": f"Add lecture: {subject_name} - {topic_name} ({date_str})",
            "tree": new_tree_sha,
            "parents": [parent_commit_sha],
        }
        r_new_commit = client.post(commits_url, json=commit_payload)
        r_new_commit.raise_for_status()
        new_commit_sha = r_new_commit.json()["sha"]

        # 6. Update Ref with retry on race
        patch_ref_url = f"https://api.github.com/repos/{self.owner}/{self.repo}/git/refs/heads/{self.branch}"
        for attempt in range(3):
            r_patch = client.patch(patch_ref_url, json={"sha": new_commit_sha, "force": False})
            if r_patch.status_code in (409, 422) and attempt < 2:
                logger.warning(
                    f"Ref update collision ({r_patch.status_code}). Re-parenting commit (attempt {attempt + 1}/3)..."
                )
                time.sleep(1.5)
                r_latest = client.get(ref_url)
                if r_latest.status_code == 200:
                    latest_parent = r_latest.json()["object"]["sha"]
                    commit_payload["parents"] = [latest_parent]
                    r_new_commit = client.post(commits_url, json=commit_payload)
                    if r_new_commit.status_code == 201:
                        new_commit_sha = r_new_commit.json()["sha"]
                        continue
            r_patch.raise_for_status()
            logger.info(
                f"Successfully published lecture in a single atomic Git commit ({new_commit_sha[:7]})!"
            )
            return True

        return False

    def _publish_remote_contents_api(
        self,
        client: httpx.Client,
        bundle_rel_dir: str,
        note_rel_path: str,
        slides_rel_dir: str,
        attachments_rel_dir: str,
        subject_slug: str,
        md_content: str,
        slide_files: List[Path],
        attachment_files: List[Path],
        analysis_result: LectureAnalysisResult,
        date_str: str,
    ):
        """Pushes files sequentially using GitHub Contents API (fallback with 409 retry)."""
        # 1. Upload unique slide images to slides/
        for sf in slide_files:
            if sf.exists():
                img_remote_path = f"{slides_rel_dir}/{sf.name}"
                self._create_or_update_file(
                    client,
                    path=img_remote_path,
                    content_bytes=sf.read_bytes(),
                    commit_msg=f"Add slide: {sf.name}",
                )

        # 2. Upload attachments if present
        for af in attachment_files:
            if af.exists():
                att_remote_path = f"{attachments_rel_dir}/{af.name}"
                self._create_or_update_file(
                    client,
                    path=att_remote_path,
                    content_bytes=af.read_bytes(),
                    commit_msg=f"Add attachment: {af.name}",
                )

        # 3. Upload Markdown note (bundle README.md)
        self._create_or_update_file(
            client,
            path=note_rel_path,
            content_bytes=md_content.encode("utf-8"),
            commit_msg=f"Add lecture note: {analysis_result.longread.topic}",
        )

        # 4. Update Subject README.md
        subj_readme_path = f"{subject_slug}/README.md"
        r_subj = client.get(
            f"https://api.github.com/repos/{self.owner}/{self.repo}/contents/{subj_readme_path}",
            params={"ref": self.branch},
        )
        current_subj_readme = ""
        subj_sha = None
        if r_subj.status_code == 200:
            d = r_subj.json()
            subj_sha = d.get("sha")
            current_subj_readme = base64.b64decode(d.get("content", "")).decode("utf-8")

        lecture_folder = bundle_rel_dir.split("/")[-1]
        updated_subj = self._update_subject_readme(
            current_subj_readme, analysis_result, lecture_folder, date_str
        )
        self._create_or_update_file(
            client,
            path=subj_readme_path,
            content_bytes=updated_subj.encode("utf-8"),
            commit_msg=f"Update subject index: {analysis_result.longread.topic}",
            sha=subj_sha,
        )

        # 5. Update root README.md
        readme_url = f"https://api.github.com/repos/{self.owner}/{self.repo}/contents/README.md"
        r = client.get(readme_url, params={"ref": self.branch})
        current_readme = ""
        readme_sha = None
        if r.status_code == 200:
            data = r.json()
            readme_sha = data.get("sha")
            current_readme = base64.b64decode(data.get("content", "")).decode("utf-8")

        updated_readme = self._update_root_readme_index(
            current_readme, analysis_result, bundle_rel_dir, date_str
        )
        self._create_or_update_file(
            client,
            path="README.md",
            content_bytes=updated_readme.encode("utf-8"),
            commit_msg=f"Update lecture index: {analysis_result.longread.topic}",
            sha=readme_sha,
        )

    def _create_or_update_file(
        self,
        client: httpx.Client,
        path: str,
        content_bytes: bytes,
        commit_msg: str,
        sha: Optional[str] = None,
        max_retries: int = 4,
    ):
        """Creates or updates a single file in the GitHub repo via API with 409 Conflict retry."""
        url = f"https://api.github.com/repos/{self.owner}/{self.repo}/contents/{path}"

        for attempt in range(max_retries):
            current_sha = sha
            if not current_sha or attempt > 0:
                check_res = client.get(url, params={"ref": self.branch})
                if check_res.status_code == 200:
                    current_sha = check_res.json().get("sha")
                elif check_res.status_code == 404:
                    current_sha = None

            payload = {
                "message": commit_msg,
                "content": base64.b64encode(content_bytes).decode("ascii"),
                "branch": self.branch,
            }
            if current_sha:
                payload["sha"] = current_sha

            resp = client.put(url, json=payload)
            if resp.status_code == 409 and attempt < max_retries - 1:
                wait_sec = 2.0 * (attempt + 1)
                logger.warning(
                    f"409 Conflict updating {path} (attempt {attempt + 1}/{max_retries}). "
                    f"Retrying in {wait_sec}s..."
                )
                time.sleep(wait_sec)
                continue
            resp.raise_for_status()
            return

    def _update_subject_readme(
        self,
        current_readme: str,
        analysis_result: LectureAnalysisResult,
        lecture_folder_name: str,
        date_str: str,
    ) -> str:
        """Appends lecture to subject-level README table."""
        subject = analysis_result.longread.subject
        topic = analysis_result.longread.topic

        deadlines = []
        for item in analysis_result.digest.action_items:
            if item.category in ("lab", "homework", "deadline"):
                d_text = f"{item.title}" + (f" ({item.deadline})" if item.deadline else "")
                deadlines.append(d_text)
        deadline_str = "; ".join(deadlines) if deadlines else "—"

        table_row = f"| {date_str} | {topic} | {deadline_str} | [📖 Открыть]({lecture_folder_name}) |"

        header = (
            f"# 📚 {subject}\n\n"
            f"> Все конспекты лекций, слайды и материалы курса.\n\n"
            "| Дата | Тема лекции | ДЗ и Дедлайны | Конспект |\n"
            "| :--- | :--- | :--- | :--- |\n"
        )

        if not current_readme or "| Дата | Тема лекции |" not in current_readme:
            return header + table_row + "\n"

        lines = current_readme.splitlines()
        new_lines = []
        replaced = False

        # If entry for this lecture already exists, replace it to avoid duplicate rows
        target_link = f"({lecture_folder_name})"
        for line in lines:
            if target_link in line:
                new_lines.append(table_row)
                replaced = True
            else:
                new_lines.append(line)

        if replaced:
            return "\n".join(new_lines) + "\n"

        # If not present, insert below table separator
        final_lines = []
        inserted = False
        for line in new_lines:
            final_lines.append(line)
            if "| :--- | :--- |" in line and not inserted:
                final_lines.append(table_row)
                inserted = True
        if not inserted:
            final_lines.append(table_row)
        return "\n".join(final_lines) + "\n"

    def _update_root_readme_index(
        self,
        current_readme: str,
        analysis_result: LectureAnalysisResult,
        bundle_rel_path: str,
        date_str: str,
    ) -> str:
        """Appends or updates lecture entry in the root README.md table."""
        subject = analysis_result.longread.subject
        topic = analysis_result.longread.topic

        deadlines = []
        for item in analysis_result.digest.action_items:
            if item.category in ("lab", "homework", "deadline"):
                d_text = f"{item.title}" + (f" ({item.deadline})" if item.deadline else "")
                deadlines.append(d_text)
        deadline_str = "; ".join(deadlines) if deadlines else "—"

        table_row = f"| {date_str} | **{subject}** | {topic} | {deadline_str} | [📖 Читать]({bundle_rel_path}) |"

        table_header = (
            "# 📚 База знаний курса (Student Assistant)\n\n"
            "> Автоматически обновляемый репозиторий с иллюстрированными конспектами лекций, слайдами и дедлайнами.\n\n"
            "| Дата | Предмет | Тема лекции | ДЗ и Дедлайны | Конспект |\n"
            "| :--- | :--- | :--- | :--- | :--- |\n"
        )

        if not current_readme or "| Дата | Предмет |" not in current_readme:
            return table_header + table_row + "\n"

        lines = current_readme.splitlines()
        new_lines = []
        replaced = False

        # If entry for this lecture already exists, replace it to avoid duplicate rows
        target_link = f"({bundle_rel_path})"
        for line in lines:
            if target_link in line:
                new_lines.append(table_row)
                replaced = True
            else:
                new_lines.append(line)

        if replaced:
            return "\n".join(new_lines) + "\n"

        final_lines = []
        inserted = False

        for line in new_lines:
            final_lines.append(line)
            if "| :--- | :--- |" in line and not inserted:
                final_lines.append(table_row)
                inserted = True

        if not inserted:
            final_lines.append(table_row)

        return "\n".join(final_lines) + "\n"
