import requests
from requests.auth import HTTPBasicAuth
import sys


def upload_jira_attachment(jira_domain, issue_key, file_path, api_user, api_token):
    """
    向Jira问题上传附件

    参数:
        jira_domain (str): Jira域名（如xxx.atlassian.net）
        issue_key (str): Jira问题键（如PROJ-123）
        file_path (str): 本地文件路径
        api_user (str): Jira API用户名（通常是邮箱）
        api_token (str): Jira API令牌
    """
    try:
        # 构建附件上传URL
        attachment_url = f"https://{jira_domain}/rest/api/3/issue/{issue_key}/attachments"

        # 请求头设置
        headers = {
            "X-Atlassian-Token": "no-check"  # Jira要求的特殊令牌
        }

        # 打开文件并准备上传
        with open(file_path, 'rb') as file:
            files = {'file': (file.name, file)}  # 包含文件名和文件对象

            # 发送上传请求
            response = requests.post(
                attachment_url,
                headers=headers,
                files=files,
                auth=HTTPBasicAuth(api_user, api_token)
            )

            # 检查请求是否成功
            response.raise_for_status()
            print("附件上传成功")
            return True

    except FileNotFoundError:
        print(f"错误: 文件 '{file_path}' 不存在")
        return False
    except requests.exceptions.HTTPError as e:
        print(f"HTTP错误: {str(e)}")
        return False
    except Exception as e:
        print(f"上传失败: {str(e)}")
        return False


if __name__ == "__main__":
    # 当直接运行脚本时，从命令行参数获取输入
    if len(sys.argv) != 6:
        print("用法: python jira_attachment_uploader.py <jira_domain> <issue_key> <file_path> <API_USER> <API_TOKEN>")
        sys.exit(1)

    # 解析命令行参数
    jira_domain = sys.argv[1]
    issue_key = sys.argv[2]
    file_path = sys.argv[3]
    api_user = sys.argv[4]
    api_token = sys.argv[5]

    # 执行上传
    upload_jira_attachment(jira_domain, issue_key, file_path, api_user, api_token)