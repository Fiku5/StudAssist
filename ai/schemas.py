import html
from pathlib import Path
from typing import List, Optional, Literal
from pydantic import BaseModel, Field


class ActionItem(BaseModel):
    """Specific task, homework, or exam hint extracted from the lecture."""
    category: Literal["homework", "lab", "exam_hint", "deadline", "self_study"] = Field(
        description="Категория задачи: домашнее задание, лабораторная, подсказка к экзамену, дедлайн или материал для изучения"
    )
    title: str = Field(description="Краткая формулировка задачи/дедлайна")
    description: str = Field(description="Подробные условия, требования преподавателя, критерии сдачи")
    deadline: Optional[str] = Field(default=None, description="Срок сдачи (если был озвучен на паре)")
    importance: Literal["critical", "high", "normal"] = Field(
        default="high",
        description="Уровень важности"
    )
    source_quote: Optional[str] = Field(
        default=None,
        description="ОБЯЗАТЕЛЬНО: дословная цитата преподавателя из аудио или точная строчка со слайда, доказывающая этот пункт. Если цитаты/доказательства нет — создавать этот ActionItem ЗАПРЕЩЕНО!"
    )


class ActionItemsDigest(BaseModel):
    """
    Tier 1 Deliverable: Quick 1-minute read for Telegram channel immediately after class.
    """
    subject: str = Field(description="Название предмета/дисциплины")
    topic: str = Field(description="Тема сегодняшней пары")
    date: Optional[str] = Field(default=None, description="Дата проведения пары")
    executive_summary: str = Field(
        description="Сверхкраткий обзор пары за 2-3 предложения: что главное прошли"
    )
    action_items: List[ActionItem] = Field(
        description="Список всех ДЗ, требований к лабам, дедлайнов и экзаменационных акцентов"
    )
    key_technologies_and_terms: List[str] = Field(
        description="Список терминов, технологий, библиотек или алгоритмов, которые нужно изучить/погуглить"
    )
    model_name: Optional[str] = Field(
        default=None,
        description="Имя модели Gemini, сгенерировавшей этот дайджест"
    )

    def to_telegram_markdown(self, full_article_url: Optional[str] = None) -> str:
        """Formats the digest into an attractive Telegram message."""
        lines = [
            f"⚡ **{self.subject}** | *{self.topic}*",
            "",
            f"📌 **Главное в двух словах:**",
            f"{self.executive_summary}",
            "",
            "🎯 **Action Items (ДЗ, дедлайны и требования):**"
        ]

        if not self.action_items:
            lines.append("• На этой паре преподаватель не озвучил домашних заданий и дедлайнов.")
        else:
            for item in self.action_items:
                badge = "🔴" if item.importance == "critical" else "🟠" if item.importance == "high" else "🔵"
                cat_names = {
                    "homework": "ДЗ",
                    "lab": "Лабораторная",
                    "exam_hint": "Экзамен/Зачет",
                    "deadline": "Дедлайн",
                    "self_study": "Изучить"
                }
                cat_str = cat_names.get(item.category, item.category.upper())
                deadline_str = f" (Срок: {item.deadline})" if item.deadline else ""
                lines.append(f"{badge} **[{cat_str}]** {item.title}{deadline_str}")
                lines.append(f"   {item.description}")
                if item.source_quote:
                    lines.append(f"   💬 «{item.source_quote}»")

        if self.key_technologies_and_terms:
            terms_str = ", ".join([f"`{t}`" for t in self.key_technologies_and_terms])
            lines.append("")
            lines.append(f"🔍 **Что погуглить / стек:** {terms_str}")

        if self.model_name:
            lines.append("")
            lines.append(f"🧠 **AI-модель:** `{self.model_name}`")

        if full_article_url:
            lines.append("")
            lines.append(f"📖 **[Читать подробный иллюстрированный конспект]({full_article_url})**")

        return "\n".join(lines)

    def to_telegram_html(self, full_article_url: Optional[str] = None) -> str:
        """Formats the digest into a robust, rich HTML message for Telegram."""
        s_subj = html.escape(self.subject)
        s_topic = html.escape(self.topic)
        s_summary = html.escape(self.executive_summary)
        lines = [
            f"⚡ <b>{s_subj}</b> | <i>{s_topic}</i>",
            "",
            "📌 <b>Главное в двух словах:</b>",
            s_summary,
            "",
            "🎯 <b>Action Items (ДЗ, дедлайны и требования):</b>",
        ]

        if not self.action_items:
            lines.append("• На этой паре преподаватель не озвучил домашних заданий и дедлайнов.")
        else:
            for item in self.action_items:
                badge = "🔴" if item.importance == "critical" else "🟠" if item.importance == "high" else "🔵"
                cat_names = {
                    "homework": "ДЗ",
                    "lab": "Лабораторная",
                    "exam_hint": "Экзамен/Зачет",
                    "deadline": "Дедлайн",
                    "self_study": "Изучить",
                }
                cat_str = html.escape(cat_names.get(item.category, item.category.upper()))
                title = html.escape(item.title)
                desc = html.escape(item.description)
                deadline_str = f" (Срок: {html.escape(item.deadline)})" if item.deadline else ""
                lines.append(f"{badge} <b>[{cat_str}]</b> {title}{deadline_str}")
                lines.append(f"   {desc}")
                if item.source_quote:
                    quote = html.escape(item.source_quote)
                    lines.append(f"   💬 «{quote}»")

        if self.key_technologies_and_terms:
            terms_str = ", ".join([f"<code>{html.escape(t)}</code>" for t in self.key_technologies_and_terms])
            lines.append("")
            lines.append(f"🔍 <b>Что погуглить / стек:</b> {terms_str}")

        if self.model_name:
            lines.append("")
            lines.append(f"🧠 <b>AI-модель:</b> <code>{html.escape(self.model_name)}</code>")

        if full_article_url:
            lines.append("")
            lines.append(f'📖 <b><a href="{html.escape(full_article_url)}">Читать подробный иллюстрированный конспект</a></b>')

        return "\n".join(lines)


class LectureSection(BaseModel):
    """A distinct thematic section of the detailed longread."""
    heading: str = Field(description="Заголовок раздела (например, 'Принцип работы Event Loop')")
    timecode: str = Field(description="Таймкод начала обсуждения (например, '14m20s')")
    content_markdown: str = Field(
        description="Структурированный текст раздела в Markdown: ясный язык учебника, чистые блоки кода, вырезанная вода и исправленные оговорки препода"
    )
    referenced_slide_filenames: List[str] = Field(
        default_factory=list,
        description="Имена файлов слайдов/скриншотов, которые относятся к этому разделу (например, ['slide_002_14m20s.png'])"
    )
    exam_cues: List[str] = Field(
        default_factory=list,
        description="Авторские акценты лектора, нюансы и формулировки, важные для экзамена/зачета"
    )


class IllustratedLongread(BaseModel):
    """
    Tier 2 Deliverable: Comprehensive structured textbook note for GitHub Markdown repository.
    """
    title: str = Field(description="Полный заголовок конспекта")
    subject: str = Field(description="Дисциплина")
    topic: str = Field(description="Тема лекции")
    overview: str = Field(description="Введение и контекст темы")
    sections: List[LectureSection] = Field(description="Разделы лекции с кодом и иллюстрациями")
    model_name: Optional[str] = Field(
        default=None,
        description="Имя модели Gemini, сгенерировавшей этот конспект"
    )

    def to_markdown_document(self, assets_relative_path: str = "assets") -> str:
        """Renders the entire longread into GitHub Flavored Markdown."""
        model_line = f"\n> **AI-модель**: `{self.model_name}`  " if self.model_name else ""
        doc = [
            f"# {self.subject}: {self.topic}",
            "",
            f"> **Тема**: {self.topic}  ",
            f"> **Статус конспекта**: Очищен от оговорок и воды, структурирован AI-ассистентом с сохранением авторских терминов.  {model_line}",
            "",
            "## Введение",
            self.overview,
            "",
            "---",
            ""
        ]

        for sec in self.sections:
            doc.append(f"## {sec.heading} `[{sec.timecode}]`")
            doc.append("")
            doc.append(sec.content_markdown)
            doc.append("")

            # Embed screenshots and attachments if present
            if sec.referenced_slide_filenames:
                doc.append("### Слайды и код с экрана:")
                for img_name in sec.referenced_slide_filenames:
                    clean_name = Path(img_name).name
                    if not clean_name.startswith("slide_"):
                        doc.append(f"![{clean_name}](attachments/{clean_name})")
                    else:
                        doc.append(f"![{clean_name}]({assets_relative_path}/{clean_name})")
                doc.append("")

            # Exam hooks
            if sec.exam_cues:
                doc.append("> [!IMPORTANT]")
                doc.append("> **На заметку к зачету / экзамену:**")
                for cue in sec.exam_cues:
                    doc.append(f"> - {cue}")
                doc.append("")

            doc.append("---")
            doc.append("")

        return "\n".join(doc)

    format_markdown = to_markdown_document
    to_markdown = to_markdown_document


class LectureAnalysisResult(BaseModel):
    """Master result containing both Tier 1 and Tier 2 outputs."""
    digest: ActionItemsDigest
    longread: IllustratedLongread
    model_name: Optional[str] = Field(default=None, description="Имя модели Gemini, выполнившей генерацию")
