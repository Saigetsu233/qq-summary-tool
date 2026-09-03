import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from qq_summary import (
    AliasBook,
    QQExportDatabase,
    QQSummaryError,
    _chat_completion,
    _deepseek_chat,
    to_plain_text,
)


class QQExportDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "nt_msg_export.db"
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            """
            CREATE TABLE group_messages (
                msg_id INTEGER PRIMARY KEY,
                timestamp INTEGER NOT NULL,
                direction INTEGER NOT NULL,
                sender_uid TEXT NOT NULL,
                sender_qq INTEGER,
                group_id TEXT NOT NULL,
                group_qq INTEGER NOT NULL,
                msg_type INTEGER NOT NULL,
                subtype INTEGER,
                content_type INTEGER,
                text TEXT,
                parse_status TEXT NOT NULL,
                content TEXT
            )
            """
        )
        rows = [
            (1, 1000, 0, "uid-a", 10001, "20001", 20001, 1, 0, 1, "第一条", "typed", "{}"),
            (2, 1001, 0, "uid-b", 10002, "20001", 20001, 1, 0, 1, "第二条", "typed", "{}"),
            (3, 1002, 0, "uid-a", 10001, "20001", 20001, 1, 0, 1, "第三条", "typed", "{}"),
            (4, 1003, 0, "uid-c", 10003, "20002", 20002, 1, 0, 1, "另一个群", "typed", "{}"),
            (5, 1004, 0, "uid-a", 10001, "20001", 20001, 1, 0, 1, "", "typed", "{}"),
        ]
        connection.executemany(
            "INSERT INTO group_messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
        )
        connection.commit()
        connection.close()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_lists_groups_and_preserves_sender_attribution(self):
        aliases = AliasBook(
            {
                "groups": {"20001": "测试群"},
                "members": {"20001": {"10001": "阿雪", "10002": "小明"}},
            }
        )
        database = QQExportDatabase(self.db_path, aliases=aliases)
        try:
            groups = database.list_groups()
            self.assertEqual(groups[0].display_name, "测试群")
            self.assertEqual(groups[0].message_count, 3)
            messages = database.get_messages("20001", 0, 2000)
            self.assertEqual(len(messages), 3)
            self.assertIn("阿雪：第一条", messages[0])
            self.assertIn("小明：第二条", messages[1])
            self.assertNotIn("10001", "\n".join(messages))
            self.assertNotIn("uid-a", "\n".join(messages))
        finally:
            database.close()

    def test_falls_back_to_stable_anonymous_labels(self):
        database = QQExportDatabase(self.db_path)
        try:
            messages = database.get_messages("20001", 0, 2000)
            self.assertEqual(messages[0].split("：", 1)[0].split()[-1], "群友1")
            self.assertTrue(messages[2].split("：", 1)[0].endswith("群友1"))
            self.assertTrue(messages[1].split("：", 1)[0].endswith("群友2"))
        finally:
            database.close()


class OutputAndApiTests(unittest.TestCase):
    def test_markdown_is_cleaned_for_qq(self):
        value = "# 标题\n\n**内容**\n\n| 成就 | 人物 |\n| --- | --- |\n| MVP | 小明 |"
        result = to_plain_text(value)
        self.assertNotIn("#", result)
        self.assertNotIn("**", result)
        self.assertNotIn("| ---", result)
        self.assertIn("1、MVP｜小明", result)

    @mock.patch("qq_summary.requests.post")
    def test_402_has_friendly_error(self, post):
        response = mock.Mock(status_code=402)
        post.return_value = response
        with self.assertRaisesRegex(QQSummaryError, "余额不足"):
            _deepseek_chat("test-key", "test")

    @mock.patch("qq_summary.requests.post")
    def test_nvidia_uses_compatible_endpoint_and_selected_model(self, post):
        response = mock.Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [{"message": {"content": "NVIDIA 总结结果"}}]
        }
        post.return_value = response

        result = _chat_completion(
            "nvidia-test-key",
            "test",
            provider="nvidia",
            model="deepseek-ai/deepseek-v4-pro-0813",
        )

        self.assertEqual(result, "NVIDIA 总结结果")
        _args, kwargs = post.call_args
        self.assertEqual(
            kwargs["json"]["model"], "deepseek-ai/deepseek-v4-pro-0813"
        )
        self.assertEqual(
            post.call_args.args[0],
            "https://integrate.api.nvidia.com/v1/chat/completions",
        )
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer nvidia-test-key")

    @mock.patch("qq_summary.requests.post")
    def test_nvidia_rate_limit_has_friendly_error(self, post):
        post.return_value = mock.Mock(status_code=429)
        with self.assertRaisesRegex(QQSummaryError, "频率或额度限制"):
            _chat_completion("test-key", "test", provider="nvidia")


if __name__ == "__main__":
    unittest.main()
