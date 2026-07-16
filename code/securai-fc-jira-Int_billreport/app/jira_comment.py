import requests
from requests.auth import HTTPBasicAuth
import logging
import time


def add_jira_comment(jira_domain, issue_key, username, api_token, comment_body):
    """
    向指定的 Jira Issue 添加评论 (支持网络超时重试)。

    :param jira_domain: Jira 域名 (例如: securai.atlassian.net)
    :param issue_key: Jira 问题 Key (例如: TLACSOBA-63)
    :param username: 登录用户名/邮箱
    :param api_token: Jira API Token
    :param comment_body: 评论内容文本
    :return: (是否成功: bool, 状态码: int/None, 消息: str)
    """
    logger = logging.getLogger(__name__)

    # 构建 Jira REST API v3 评论接口 URL
    url = f"https://{jira_domain}/rest/api/3/issue/{issue_key}/comment"

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    # 构建 Jira API v3 要求的 ADF (Atlassian Document Format) 格式 Payload
    payload = {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": comment_body
                        }
                    ]
                }
            ]
        }
    }

    max_retries = 3  # 最大重试次数

    for attempt in range(max_retries):
        try:
            if attempt == 0:
                logger.info(f"API请求URL：{url}")
                logger.info("发起评论添加请求...")

            # 发起 POST 请求，加入 timeout=(连接超时, 读取超时)
            response = requests.post(
                url,
                json=payload,
                headers=headers,
                auth=HTTPBasicAuth(username, api_token),
                timeout=(5, 15)  # 5秒连不上就断，15秒等不到响应就断
            )

            response.raise_for_status()

            # 请求成功，直接返回
            return True, response.status_code, "Success"

        except requests.exceptions.RequestException as e:
            # 捕获 requests 相关的网络异常 (超时、连接重置等)
            if attempt < max_retries - 1:
                logger.warning(
                    f"⚠️ 问题 {issue_key} 评论添加遇到网络异常: {str(e).split('Caused by')[-1][:100]}... 等待2秒后进行第 {attempt + 2} 次重试...")
                time.sleep(2)  # 等待 2 秒后重试
            else:
                error_msg = str(e)
                logger.error(f"❌ 问题 {issue_key} 评论添加异常：发生异常: {error_msg}")
                return False, None, error_msg

        except Exception as e:
            # 捕获其他未知异常，不进行重试，直接退出
            error_msg = str(e)
            logger.error(f"❌ 问题 {issue_key} 评论添加发生未知异常: {error_msg}")
            return False, None, error_msg

    # 理论上不会走到这里，为了代码健壮性兜底
    return False, None, "Unknown Error"