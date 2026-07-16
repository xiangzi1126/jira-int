import requests
from requests.auth import HTTPBasicAuth
import configparser
import os
import csv
import logging
from datetime import datetime


# 初始化日志配置
def init_logger():
    """初始化日志，输出到 ../../jira/log/renewal.log 和控制台"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.normpath(os.path.join(current_dir, '../../jira/log'))
    os.makedirs(log_dir, exist_ok=True)  # 自动创建log目录
    # 修改点1：日志文件名改为 renewal.log
    log_file = os.path.join(log_dir, 'renewal.log')

    # 配置日志格式（时间戳+级别+信息）
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),  # 写入文件（追加模式）
            logging.StreamHandler()  # 输出到控制台
        ]
    )
    return logging.getLogger(__name__)


# 初始化日志实例
logger = init_logger()


def get_jira_issue_data():
    logger.info("=" * 50)
    logger.info(f"开始执行Jira续费工单数据查询（时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}）")

    # 获取当前脚本所在目录
    current_dir = os.path.dirname(os.path.abspath(__file__))

    # 拼接数据文件路径
    data_dir = os.path.normpath(os.path.join(current_dir, '../../jira/data'))
    os.makedirs(data_dir, exist_ok=True)  # 确保data目录存在
    # 修改点2：输出CSV文件名改为 jira_get_renewal_issues.csv
    data_file = os.path.join(data_dir, 'jira_get_renewal_issues.csv')

    # 拼接配置文件路径
    config_path = os.path.join(current_dir, '../../jira/config/jira_config.ini')
    config_path = os.path.normpath(config_path)

    # 检查配置文件是否存在
    if not os.path.exists(config_path):
        err_msg = f"配置文件不存在 - {config_path}"
        logger.error(err_msg)
        print(err_msg)
        return

    # 读取配置文件
    config = configparser.ConfigParser()
    try:
        config.read(config_path, encoding='utf-8')
        logger.info(f"已加载配置文件：{config_path}")
        print(f"已加载配置文件：{config_path}")
    except Exception as e:
        err_msg = f"读取配置文件失败：{str(e)}"
        logger.error(err_msg)
        print(err_msg)
        return

    try:
        # 从配置文件获取认证信息、域名
        jira_domain = config.get('jira', 'domain')
        user_name = config.get('jira', 'user_name')
        access_token = config.get('jira', 'access_token')
        logger.info(f"成功读取Jira配置 - 域名：{jira_domain}，用户名：{user_name}")

        # 修改点3：更新JQL查询语句为续费相关条件
        jql = 'type = 续费 AND "账号所属平台[dropdown]" = "阿里国际"'
        # 调整查询字段：去掉标签对应的customfield_10646
        fields = "key,project,customfield_10484,customfield_10493"
        logger.info(f"生成查询JQL：{jql}")
        logger.info(f"查询字段：{fields}")

        # Jira REST API URL
        url = f"https://{jira_domain}/rest/api/3/search/jql"
        logger.info(f"API请求URL：{url}")

        # 请求参数
        params = {
            "jql": jql,
            "fields": fields,
            "maxResults": 100  # 可根据需要调整，最大1000
        }

        # 发起请求
        logger.info("发起Jira API请求...")
        response = requests.get(
            url,
            params=params,
            auth=HTTPBasicAuth(user_name, access_token)
        )
        response.raise_for_status()
        data = response.json()
        logger.info(f"API请求成功，返回 {len(data.get('issues', []))} 条记录")

        logger.info(f"数据将覆盖保存到：{data_file}")

        # 写入CSV文件（覆盖模式）
        with open(data_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            # 修改点4：CSV表头去掉标签列
            writer.writerow(['Issue Key', 'Project Key', '资源所属账号', '账号所属平台'])
            count = 0
            for issue in data["issues"]:
                issue_key = issue["key"]
                # 从project字段中提取项目键（key属性）
                project_key = issue["fields"].get("project", {}).get("key", "")
                resource_account = issue["fields"].get("customfield_10484") or ""  # 资源所属账号

                # 提取账号所属平台的value值（如"阿里国际"）
                account_platform_data = issue["fields"].get("customfield_10493")
                account_platform = account_platform_data.get("value", "") if isinstance(account_platform_data,
                                                                                        dict) else ""

                # 修改点5：写入行去掉标签相关内容
                writer.writerow([issue_key, project_key, resource_account, account_platform])
                count += 1
        logger.info(f"数据写入完成，共 {count} 条记录（已覆盖原有文件）")
        print(f"数据已成功覆盖写入文件：{data_file}，共 {count} 条记录")

    except configparser.NoSectionError:
        err_msg = "配置文件中未找到 [jira] 段落，请检查文件结构"
        logger.error(err_msg)
        print(err_msg)
    except configparser.NoOptionError as e:
        err_msg = f"配置文件中缺少参数 - {e}，请检查键名是否正确"
        logger.error(err_msg)
        print(err_msg)
    except requests.exceptions.HTTPError as e:
        err_msg = f"HTTP错误：{e}"
        logger.error(f"{err_msg}，响应详情：{response.text if 'response' in locals() else '无'}")
        print(err_msg)
    except KeyError as e:
        err_msg = f"响应数据解析错误：缺少键 {e}"
        logger.error(err_msg)
        print(err_msg)
    except Exception as e:
        err_msg = f"其他错误：{str(e)}"
        logger.error(err_msg, exc_info=True)  # exc_info=True记录堆栈信息
        print(err_msg)
    finally:
        logger.info("Jira续费工单数据查询流程结束")
        logger.info("=" * 50 + "\n")


if __name__ == "__main__":
    get_jira_issue_data()