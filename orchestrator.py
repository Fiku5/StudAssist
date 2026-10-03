import asyncio
import logging
from pathlib import Path
from typing import List, Optional, Union

from aiogram import Bot
from config import settings, TEMP_DIR
from recorder.visual import SlideDeduplicator
from ai.analyzer import LectureAnalyzer
from publisher.github_sync import GitHubPublisher
from publisher.telegram_pub import TelegramPublisher

logger = logging.getLogger(__name__)


class LectureOrchestrator:
    """
    Master coordinator of the StudentAssistant lecture pipeline:
    1. Deduplicates visual slides via pHash.
    2. Runs multimodal analysis via Gemini 3.8 Flash.
    3. Publishes Markdown and assets to GitHub.
    4. Posts 1-minute Action Items digest to Telegram channel.
    """

    def __init__(
        self,
        analyzer: Optional[LectureAnalyzer] = None,
        github_pub: Optional[GitHubPublisher] = None,
        telegram_pub: Optional[TelegramPublisher] = None,
        bot: Optional[Bot] = None,
    ):
        self.analyzer = analyzer or LectureAnalyzer()
        self.github_pub = github_pub or GitHubPublisher()
        self.telegram_pub = telegram_pub or TelegramPublisher(bot=bot)

    async def close(self):
        """Closes any resources owned by the orchestrator."""
        if self.telegram_pub:
            await self.telegram_pub.close()

    async def run_pipeline(
        self,
        audio_path: Optional[Union[str, Path]] = None,
        slide_paths: Optional[List[Union[str, Path]]] = None,
        raw_slide_paths: Optional[List[Union[str, Path]]] = None,
        attachment_paths: Optional[List[Union[str, Path]]] = None,
        subject_hint: str = "",
        assets_output_dir: Optional[Path] = None,
        is_audio_silent: bool = False,
        force_reanalyze: bool = False,
        publish_to_telegram: bool = True,
        telegram_thread_id: Optional[int] = None,
        with_telegram_buttons: bool = True,
        lecture_date: Optional[str] = None,
        subject_title: Optional[str] = None,
    ) -> dict:
        """Executes the complete processing and publishing pipeline."""
        assets_dir = assets_output_dir or (TEMP_DIR / "unique_slides")
        assets_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"=== Starting Lecture Pipeline for subject: '{subject_hint}' ===")

        # 1. Slide Preparation / Deduplication
        unique_slide_files: List[Path] = []
        if slide_paths:
            logger.info(f"Using {len(slide_paths)} pre-processed slide frames directly...")
            unique_slide_files = [Path(p) for p in slide_paths if Path(p).exists()]
        elif raw_slide_paths:
            logger.info(f"Deduplicating {len(raw_slide_paths)} raw visual frames...")
            dedup = SlideDeduplicator(
                output_dir=assets_dir,
                similarity_threshold=settings.slide_similarity_threshold,
            )
            for idx, p in enumerate(raw_slide_paths):
                # Assume 5-second interval if timestamps not explicitly given
                sec = idx * settings.slide_capture_interval_sec
                dedup.process_frame(p, timestamp_sec=sec)
            unique_slide_files = [s.file_path for s in dedup.saved_slides]
            logger.info(f"Retained {len(unique_slide_files)} unique slides.")

        # 2. AI Multimodal Analysis
        # Check if session directory has cached analysis to avoid redundant Gemini calls on retry (unless force_reanalyze is True)
        session_cache_file: Optional[Path] = None
        if audio_path and Path(audio_path).parent.exists():
            session_cache_file = Path(audio_path).parent / "analysis_cache.json"
        elif unique_slide_files and unique_slide_files[0].parent.parent.exists():
            potential_sess_dir = unique_slide_files[0].parent.parent
            if (potential_sess_dir / "metadata.json").exists():
                session_cache_file = potential_sess_dir / "analysis_cache.json"

        analysis_result = None
        if not force_reanalyze and session_cache_file and session_cache_file.exists():
            try:
                from ai.schemas import LectureAnalysisResult
                cached_data = session_cache_file.read_text(encoding="utf-8")
                analysis_result = LectureAnalysisResult.model_validate_json(cached_data)
                logger.info(f"Loaded cached AI analysis result from {session_cache_file.name}, skipping Gemini call.")
            except Exception as e:
                logger.warning(f"Could not load cached analysis ({e}), proceeding with full AI analysis...")
                analysis_result = None

        if not analysis_result:
            logger.info(f"Running multimodal AI analysis ({self.analyzer.model_name})...")
            analysis_result = await asyncio.to_thread(
                self.analyzer.analyze,
                audio_path=audio_path,
                slide_paths=unique_slide_files,
                attachment_paths=attachment_paths,
                subject_hint=subject_hint,
                is_audio_silent=is_audio_silent,
            )
            if session_cache_file:
                try:
                    session_cache_file.write_text(analysis_result.model_dump_json(indent=2), encoding="utf-8")
                    logger.info(f"Cached AI analysis result to {session_cache_file.name}")
                except Exception as e:
                    logger.warning(f"Failed to cache AI analysis result: {e}")

        # 3. Publish to GitHub
        logger.info("Publishing lecture notes to GitHub repository...")
        if attachment_paths and Path(attachment_paths[0]).parent.exists() and Path(attachment_paths[0]).parent.name == "attachments":
            att_files = sorted([f for f in Path(attachment_paths[0]).parent.iterdir() if f.is_file() and f.stat().st_size > 0])
        else:
            att_files = [Path(p) for p in (attachment_paths or []) if Path(p).exists()]
        github_url = await asyncio.to_thread(
            self.github_pub.publish_lecture,
            analysis_result=analysis_result,
            slide_files=unique_slide_files,
            attachment_files=att_files,
            lecture_date=lecture_date,
            subject_name=subject_title,
        )
        logger.info(f"Published to GitHub: {github_url}")

        # 4. Publish to Telegram (Channel or Supergroup Forum Topic)
        message_id = None
        if publish_to_telegram:
            logger.info("Publishing Action Items to Telegram...")
            target_chat = settings.telegram_group_id if telegram_thread_id else (self.telegram_pub.channel_id or settings.telegram_group_id)
            message_id = await self.telegram_pub.publish_to_channel(
                digest=analysis_result.digest,
                full_article_url=github_url,
                chat_id=target_chat,
                message_thread_id=telegram_thread_id,
                with_keyboard=with_telegram_buttons,
            )

        logger.info("=== Lecture Pipeline Completed Successfully ===")

        return {
            "subject": analysis_result.longread.subject,
            "topic": analysis_result.longread.topic,
            "github_url": github_url,
            "telegram_message_id": message_id,
            "unique_slides_count": len(unique_slide_files),
            "action_items_count": len(analysis_result.digest.action_items),
            "is_audio_silent": is_audio_silent,
            "result": analysis_result,
        }

    def run_pipeline_sync(self, *args, **kwargs) -> dict:
        """Synchronous wrapper for run_pipeline."""
        return asyncio.run(self.run_pipeline(*args, **kwargs))
