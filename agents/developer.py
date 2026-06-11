from .base import Agent, PROJECT_DIR, GITHUB_REPO
from config import TIMEOUT_DEVELOPER


class DeveloperAgent(Agent):
    def __init__(self):
        super().__init__("Developer")

    def fix(self, issue_number: int, continue_session: bool = False) -> str:
        prompt = f"修复 Issue #{issue_number}，如果需要更多信息请自行获取"
        return self.run(prompt, timeout=TIMEOUT_DEVELOPER, continue_session=continue_session)

    def apply_review(self, issue_number: int, pr_url: str, continue_session: bool = False) -> str:
        if pr_url:
            prompt = (
                f"PR {pr_url} 有新的 CodeRabbitAI Code Review，用 gh pr view --comments 读取完整 review。"
                f"如果 review 已认可且无需修改，直接输出 NO_ACTION_NEEDED。"
                f"如果 review 有修改意见，修改代码并本地提交，但先不要 push"
            )
        else:
            prompt = "根据 Reviewer 的本地 review 意见修改代码并提交"
        return self.run(prompt, timeout=TIMEOUT_DEVELOPER, continue_session=continue_session)

    def push_and_notify(self, pr_url: str, continue_session: bool = False) -> str:
        prompt = f"将当前分支的修改 push 到 remote，然后在 PR {pr_url} 中评论 @coderabbitai 说明修改了什么，评论请使用英文"
        return self.run(prompt, timeout=600, continue_session=continue_session)
