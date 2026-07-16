import requests
from requests.auth import HTTPBasicAuth
import configparser
import os
import csv
import logging
from datetime import datetime, timedelta


# 初始化日志配置
def init_logger():
    """初始化日志，输出到 ../../jira/log/jira_get_alert.log 和控制台"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.normpath(os.path.join(current_dir, '../../jira/log'))
    os.makedirs(log_dir, exist_ok=True)  # 自动创建log目录
    log_file = os.path.join(log_dir, 'jira_get_alert.log')

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


def get_last_two_months_date_range():
    """动态获取上两个月的第一天和上个月的最后一天（共包含两个完整的自然月）"""
    today = datetime.now()
    # 本月第一天（例如当前是 2026-07-08，则为 2026-07-01）
    first_day_this_month = today.replace(day=1)
    # 上个月最后一天（例如：2026-06-30）
    last_day_last_month = first_day_this_month - timedelta(days=1)

    # 上个月第一天（例如：2026-06-01）
    first_day_last_month = last_day_last_month.replace(day=1)
    # 上上个月最后一天（例如：2026-05-31）
    last_day_two_months_ago = first_day_last_month - timedelta(days=1)
    # 上上个月第一天（例如：2026-05-01）
    first_day_two_months_ago = last_day_two_months_ago.replace(day=1)

    return first_day_two_months_ago.strftime("%Y-%m-%d"), last_day_last_month.strftime("%Y-%m-%d")


def get_jira_alert_data():
    logger.info("=" * 50)
    logger.info(f"开始执行Jira告警数据查询（时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}）")

    # 获取当前脚本所在目录
    current_dir = os.path.dirname(os.path.abspath(__file__))

    # 拼接数据文件路径
    data_dir = os.path.normpath(os.path.join(current_dir, '../../jira/data'))
    os.makedirs(data_dir, exist_ok=True)  # 确保data目录存在

    # 结果输出文件
    data_file = os.path.join(data_dir, 'jira_get_alert.csv')

    # 拼接CSV文件路径（存放Project Key的文件）
    project_csv_path = os.path.join(data_dir, 'op_resale_business_details.csv')
    project_csv_path = os.path.normpath(project_csv_path)

    # 从CSV文件读取Project Key列
    try:
        if not os.path.exists(project_csv_path):
            err_msg = f"Project Key来源文件不存在 - {project_csv_path}"
            logger.error(err_msg)
            print(err_msg)
            return

        project_keys = []
        with open(project_csv_path, 'r', encoding='utf-8', newline='') as f:
            reader = csv.DictReader(f)
            # 检查是否存在Project Key列
            if 'Project Key' not in reader.fieldnames:
                err_msg = f"CSV文件中不存在'Project Key'列 - {project_csv_path}"
                logger.error(err_msg)
                print(err_msg)
                return

            # 读取列数据并去重、去空
            for row in reader:
                key = row['Project Key'].strip()
                if key and key not in project_keys:
                    project_keys.append(key)

        if not project_keys:
            err_msg = f"CSV文件中未找到有效的Project Key数据 - {project_csv_path}"
            logger.error(err_msg)
            print(err_msg)
            return

        logger.info(f"从CSV文件读取到有效Project Key：{project_keys}（共{len(project_keys)}个）")

    except Exception as e:
        err_msg = f"读取Project Key CSV文件失败：{str(e)}"
        logger.error(err_msg, exc_info=True)
        print(err_msg)
        return

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

        # 获取上两个月的日期范围
        start_date, end_date = get_last_two_months_date_range()
        logger.info(f"动态计算查询时间范围（上两个月）：{start_date} 至 {end_date}")

        # 动态生成JQL（多个项目用OR连接）
        project_conditions = [f"project = {key}" for key in project_keys]
        project_jql = f"({' OR '.join(project_conditions)})"

        # 组装最终的 JQL（状态排除 "静观" 和 "完成（复旧）"）
        jql = f'{project_jql} AND type = 告警邮件 AND status NOT IN ("静观", "完成（复旧）") AND created >= "{start_date}" AND created <= "{end_date}"'

        # 查询字段：只获取指定的4个自定义告警字段
        fields = "key,project,customfield_10523,customfield_10531,customfield_10532,customfield_10524"

        logger.info(f"生成查询JQL：{jql}")
        logger.info(f"查询字段：{fields}")

        # Jira REST API URL
        url = f"https://{jira_domain}/rest/api/3/search/jql"

        # 请求参数
        params = {
            "jql": jql,
            "fields": fields,
            "maxResults": 100  # 可根据实际单次返回上限需要调整
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
            # 写入定制的表头
            writer.writerow(['Issue Key', 'Project Key', '告警目标', '告警简述', '告警原因', '告警检测日时'])
            count = 0

            for issue in data.get("issues", []):
                issue_key = issue["key"]
                issue_fields = issue.get("fields", {})
                project_key = issue_fields.get("project", {}).get("key", "")

                # 辅助函数：安全提取自定义字段的值，防止返回字典或对象结构
                def get_cf_value(cf_key):
                    val = issue_fields.get(cf_key)
                    if isinstance(val, dict):
                        return val.get("value", val.get("name", str(val)))
                    return val if val else ""

                # 提取指定的四个自定义字段数据
                target = get_cf_value("customfield_10523")  # 告警目标
                summary_desc = get_cf_value("customfield_10531")  # 告警简述
                reason = get_cf_value("customfield_10532")  # 告警原因
                detect_time = get_cf_value("customfield_10524")  # 告警检测日时

                # 写入行
                writer.writerow([issue_key, project_key, target, summary_desc, reason, detect_time])
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
        logger.error(err_msg, exc_info=True)
        print(err_msg)
    finally:
        logger.info("Jira告警数据查询流程结束")
        logger.info("=" * 50 + "\n")


if __name__ == "__main__":
    get_jira_alert_data()