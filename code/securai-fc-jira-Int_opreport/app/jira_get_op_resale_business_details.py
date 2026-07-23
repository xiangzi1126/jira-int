import requests
from requests.auth import HTTPBasicAuth
import configparser
import os
import csv
import logging
from datetime import datetime


# 初始化日志配置
def init_logger():
    """初始化日志，输出到 ../../jira/log/get_business_info.log 和控制台"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.normpath(os.path.join(current_dir, '../../jira/log'))
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'get_business_info.log')

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)


logger = init_logger()


def get_all_project_keys(jira_domain, user_name, access_token):
    """获取Jira所有项目的project_key（仅项目名以【开头的）"""
    logger.info("开始获取所有项目的project_key（仅项目名以【开头的）...")
    url = f"https://{jira_domain}/rest/api/3/project"

    try:
        response = requests.get(
            url,
            auth=HTTPBasicAuth(user_name, access_token),
            headers={"Accept": "application/json"}
        )
        response.raise_for_status()
        projects = response.json()

        # 提取项目名以【开头的项目的key（去重）
        project_keys = []
        filtered_projects = []

        for project in projects:
            project_name = project.get('name', '')
            project_key = project.get('key', '').strip()

            if project_key and project_name.startswith('【'):
                filtered_projects.append({'name': project_name, 'key': project_key})
                project_keys.append(project_key)

        # 去重并过滤空值
        project_keys = list(set(project_keys))
        project_keys = [key for key in project_keys if key]

        if filtered_projects:
            logger.info(f"成功筛选出 {len(filtered_projects)} 个项目名以【开头的项目：")
            for proj in filtered_projects:
                logger.info(f"  - {proj['name']} ({proj['key']})")
        else:
            logger.warning("未获取到任何项目名以【开头的project_key")
            return []

        return project_keys

    except requests.exceptions.HTTPError as e:
        err_msg = f"获取项目列表失败（HTTP错误）：{e}"
        logger.error(f"{err_msg}，响应详情：{response.text if 'response' in locals() else '无'}")
        print(err_msg)
        return []
    except Exception as e:
        err_msg = f"获取项目列表失败：{str(e)}"
        logger.error(err_msg, exc_info=True)
        print(err_msg)
        return []


def update_config_with_projects(config_path, project_keys):
    """将project_key写入配置文件"""
    if not project_keys:
        logger.warning("没有可用的project_key，不更新配置文件")
        return False

    config = configparser.ConfigParser()
    # 读取现有配置（如果存在）
    if os.path.exists(config_path):
        config.read(config_path, encoding='utf-8')

    # 确保[jira]段落存在
    if 'jira' not in config.sections():
        config.add_section('jira')

    # 更新project_key配置（用逗号分隔）
    config.set('jira', 'project_key', ', '.join(project_keys))

    # 写入配置文件
    try:
        with open(config_path, 'w', encoding='utf-8') as f:
            config.write(f)
        logger.info(f"已将 {len(project_keys)} 个project_key写入配置文件：{config_path}")
        print(f"已更新配置文件：{config_path}，项目键：{', '.join(project_keys)}")
        return True
    except Exception as e:
        err_msg = f"写入配置文件失败：{str(e)}"
        logger.error(err_msg)
        print(err_msg)
        return False


def get_jira_issue_data():
    logger.info("=" * 50)
    logger.info(f"开始执行Jira业务数据查询流程（时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}）")

    current_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.normpath(os.path.join(current_dir, '../../jira/config/jira_config.ini'))

    # 第一步：读取基础配置（域名、用户名、令牌）用于获取项目列表
    config = configparser.ConfigParser()
    basic_config = {}
    try:
        if os.path.exists(config_path):
            config.read(config_path, encoding='utf-8')
            basic_config = {
                'domain': config.get('jira', 'domain'),
                'user_name': config.get('jira', 'user_name'),
                'access_token': config.get('jira', 'access_token')
            }
        else:
            # 如果配置文件不存在，提示用户先创建基础配置
            err_msg = f"配置文件不存在，请先在 {config_path} 中配置[jira]的domain、user_name、access_token"
            logger.error(err_msg)
            print(err_msg)
            return
    except (configparser.NoSectionError, configparser.NoOptionError) as e:
        err_msg = f"配置文件缺少必要参数：{e}，请检查[jira]段落是否包含domain、user_name、access_token"
        logger.error(err_msg)
        print(err_msg)
        return
    except Exception as e:
        err_msg = f"读取基础配置失败：{str(e)}"
        logger.error(err_msg)
        print(err_msg)
        return

    # 第二步：获取所有项目的project_key并更新配置文件
    project_keys = get_all_project_keys(
        jira_domain=basic_config['domain'],
        user_name=basic_config['user_name'],
        access_token=basic_config['access_token']
    )
    if not project_keys:
        err_msg = "无法获取有效的project_key（项目名以【开头的），流程终止"
        logger.error(err_msg)
        print(err_msg)
        return

    # 更新配置文件
    if not update_config_with_projects(config_path, project_keys):
        err_msg = "配置文件更新失败，流程终止"
        logger.error(err_msg)
        print(err_msg)
        return

    # 第三步：使用更新后的配置执行查询（复用原逻辑）
    try:
        # 重新读取配置（确保获取最新的project_key）
        config.read(config_path, encoding='utf-8')
        jira_domain = config.get('jira', 'domain')
        user_name = config.get('jira', 'user_name')
        access_token = config.get('jira', 'access_token')
        project_keys = [key.strip() for key in config.get('jira', 'project_key').split(',') if key.strip()]

        # 读取 opreport 排除项目配置：[opreport_filter] exclude_project 中列出的项目不记录
        # 注意：project_key 为多个流水线共用的主清单，此处仅在本脚本查询时过滤，不写回配置
        exclude_projects = []
        if config.has_option('opreport_filter', 'exclude_project'):
            exclude_projects = [p.strip() for p in config.get('opreport_filter', 'exclude_project').split(',') if p.strip()]
        if exclude_projects:
            before_count = len(project_keys)
            project_keys = [k for k in project_keys if k not in exclude_projects]
            logger.info(f"按[opreport_filter]exclude_project过滤掉 {before_count - len(project_keys)} 个项目，不记录：{exclude_projects}")

        logger.info(f"使用最新配置 - 域名：{jira_domain}，项目键数量：{len(project_keys)}")

        # 生成JQL查询条件 (已修改为新的 JQL 逻辑)
        project_conditions = [f"project = {key}" for key in project_keys]
        jql = f"({' OR '.join(project_conditions)}) AND \"request type\" IN (\"业务 :CI\", \"业务 :MSP\", \"業務 :CI\", \"業務 :MSP\") AND \"月报[dropdown]\" = 有 AND \"平台[dropdown]\" = 阿里云国际站 AND status = 运维中"

        # 包含所有需要的字段（新增税率字段）
        fields = "key,project,summary,customfield_10529,customfield_10527,customfield_10530,customfield_10528"
        logger.info(f"生成查询JQL：{jql}")
        logger.info(f"查询字段：{fields}")

        # 发起API请求
        url = f"https://{jira_domain}/rest/api/3/search/jql"
        logger.info(f"API请求URL：{url}")
        params = {
            "jql": jql,
            "fields": fields,
            "maxResults": 1000  # 适当增大最大结果数
        }

        logger.info("发起Jira API请求...")
        response = requests.get(
            url,
            params=params,
            auth=HTTPBasicAuth(user_name, access_token)
        )
        response.raise_for_status()
        data = response.json()
        total_issues = len(data.get('issues', []))
        logger.info(f"API请求成功，返回 {total_issues} 条记录")

        # 写入CSV文件 (已修改输出文件名为 op_resale_business_details.csv)
        data_dir = os.path.normpath(os.path.join(current_dir, '../../jira/data'))
        os.makedirs(data_dir, exist_ok=True)
        data_file = os.path.join(data_dir, 'op_resale_business_details.csv')
        logger.info(f"数据将保存到：{data_file}")

        with open(data_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            # 在表头中增加"Project Name"列
            writer.writerow([
                'Project Key',
                'Project Name',  # 新增项目名称列
                'Issue Key',
                'summary',
                '汇率',
                '代行费率',
                '税率',
                '是否有月报'
            ])
            count = 0
            for issue in data["issues"]:
                issue_key = issue["key"]
                # 从项目信息中同时获取项目键和项目名称
                project_info = issue["fields"].get("project", {})
                project_key = project_info.get("key", "")
                project_name = project_info.get("name", "")  # 获取项目名称
                summary = issue["fields"].get("summary", "")
                exchange_rate = issue["fields"].get("customfield_10529") or ""
                service_fee_rate = issue["fields"].get("customfield_10527") or ""
                tax_rate = issue["fields"].get("customfield_10528") or ""  # 税率

                # 处理“是否有月报”（只取value）
                monthly_report_data = issue["fields"].get("customfield_10530")
                has_monthly_report = monthly_report_data.get("value", "") if isinstance(monthly_report_data,
                                                                                        dict) else ""

                writer.writerow([
                    project_key,
                    project_name,  # 写入项目名称
                    issue_key,
                    summary,
                    exchange_rate,
                    service_fee_rate,
                    tax_rate,
                    has_monthly_report
                ])
                count += 1
        logger.info(f"数据写入完成，共 {count} 条记录")
        print(f"数据已成功写入文件：{data_file}，共 {count} 条记录")

    except Exception as e:
        err_msg = f"查询数据时发生错误：{str(e)}"
        logger.error(err_msg, exc_info=True)
        print(err_msg)
    finally:
        logger.info("Jira业务数据查询流程结束")
        logger.info("=" * 50 + "\n")


if __name__ == "__main__":
    get_jira_issue_data()