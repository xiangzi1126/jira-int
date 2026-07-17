import requests
from requests.auth import HTTPBasicAuth
import configparser
import os
import csv
import logging
from datetime import datetime


# 初始化日志配置（追加模式写入renewal.log）
def init_logger():
    """初始化日志，追加输出到 ../../jira/log/renewal.log 和控制台"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.normpath(os.path.join(current_dir, '../../jira/log'))
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'renewal.log')  # 追加到已有日志文件

    # 配置日志（追加模式，避免覆盖原有日志）
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file, mode='a', encoding='utf-8'),  # mode='a' 追加模式
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)


# 初始化日志实例
logger = init_logger()


def get_jira_subtasks():
    logger.info("=" * 50)
    logger.info(f"开始执行Jira续费子任务数据查询（时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}）")

    # 1. 定义文件路径
    current_dir = os.path.dirname(os.path.abspath(__file__))
    # 父任务Key来源文件（上一步生成的续费工单CSV）
    parent_csv_path = os.path.normpath(os.path.join(current_dir, '../../jira/data/jira_get_renewal_issues.csv'))
    # 子任务输出CSV文件路径
    output_csv_path = os.path.normpath(os.path.join(current_dir, '../../jira/data/jira_get_renewal_sbu_issues.csv'))
    # 配置文件路径
    config_path = os.path.normpath(os.path.join(current_dir, '../../jira/config/jira_config.ini'))

    # 2. 校验父任务CSV文件是否存在
    if not os.path.exists(parent_csv_path):
        err_msg = f"父任务Key来源文件不存在 - {parent_csv_path}"
        logger.error(err_msg)
        print(err_msg)
        return

    # 3. 读取父任务Issue Key列表（去重、去空）
    parent_keys = []
    try:
        with open(parent_csv_path, 'r', encoding='utf-8', newline='') as f:
            reader = csv.DictReader(f)
            # 校验是否有Issue Key列
            if 'Issue Key' not in reader.fieldnames:
                err_msg = f"父任务CSV文件中不存在'Issue Key'列 - {parent_csv_path}"
                logger.error(err_msg)
                print(err_msg)
                return

            for row in reader:
                parent_key = row['Issue Key'].strip()
                if parent_key and parent_key not in parent_keys:
                    parent_keys.append(parent_key)

        if not parent_keys:
            err_msg = f"父任务CSV文件中未找到有效的Issue Key - {parent_csv_path}"
            logger.error(err_msg)
            print(err_msg)
            return

        logger.info(f"读取到父任务Issue Key列表：{parent_keys}（共{len(parent_keys)}个）")

    except Exception as e:
        err_msg = f"读取父任务CSV文件失败：{str(e)}"
        logger.error(err_msg, exc_info=True)
        print(err_msg)
        return

    # 4. 读取Jira配置文件
    if not os.path.exists(config_path):
        err_msg = f"配置文件不存在 - {config_path}"
        logger.error(err_msg)
        print(err_msg)
        return

    config = configparser.ConfigParser()
    try:
        config.read(config_path, encoding='utf-8')
        logger.info(f"已加载Jira配置文件：{config_path}")
    except Exception as e:
        err_msg = f"读取配置文件失败：{str(e)}"
        logger.error(err_msg)
        print(err_msg)
        return

    # 5. 初始化Jira API认证信息
    try:
        jira_domain = config.get('jira', 'domain')
        user_name = config.get('jira', 'user_name')
        access_token = config.get('jira', 'access_token')
        logger.info(f"成功读取Jira配置 - 域名：{jira_domain}，用户名：{user_name}")
    except (configparser.NoSectionError, configparser.NoOptionError) as e:
        err_msg = f"配置文件解析错误：{str(e)}"
        logger.error(err_msg)
        print(err_msg)
        return

    # 6. 批量查询每个父任务的子任务
    api_url = f"https://{jira_domain}/rest/api/3/search/jql"
    all_subtasks = []  # 存储所有子任务数据
    total_subtasks = 0

    for parent_key in parent_keys:
        try:
            # 构造子任务查询JQL
            jql = f"parent = {parent_key}"
            # 指定要查询的字段（子任务Key + 自定义字段）
            fields = "key,customfield_10813,customfield_11170,customfield_10487,customfield_10500"
            params = {
                "jql": jql,
                "fields": fields,
                "maxResults": 100  # 单个父任务的子任务上限
            }

            logger.info(f"查询父任务[{parent_key}]的子任务，JQL：{jql}")
            # 发送API请求
            response = requests.get(
                api_url,
                params=params,
                auth=HTTPBasicAuth(user_name, access_token)
            )
            response.raise_for_status()
            data = response.json()
            subtasks = data.get('issues', [])

            if not subtasks:
                logger.info(f"父任务[{parent_key}]未查询到子任务")
                continue

            # 解析子任务数据
            for subtask in subtasks:
                subtask_key = subtask["key"]
                # 提取自定义字段（处理空值和数据类型）
                renewal_duration = subtask["fields"].get("customfield_10813") or ""  # 续费时长（月）
                renew_method = subtask["fields"].get("customfield_11170") or ""  # 续费方式
                id_value = subtask["fields"].get("customfield_10487") or ""  # ID
                expire_time = subtask["fields"].get("customfield_10500") or ""  # 到期时间

                # 格式化到期时间（如果是Jira的日期格式，保留原样）
                if isinstance(expire_time, dict):
                    expire_time = expire_time.get("iso8601", "")  # 兼容Jira日期字段格式

                all_subtasks.append({
                    "Issue Key": subtask_key,
                    "PARENT_KEY": parent_key,
                    "续费时长（月）": renewal_duration,
                    "续费方式": renew_method,
                    "ID": id_value,
                    "到期时间": expire_time
                })
                total_subtasks += 1

            logger.info(f"父任务[{parent_key}]查询到{len(subtasks)}个子任务")

        except requests.exceptions.HTTPError as e:
            err_msg = f"查询父任务[{parent_key}]子任务时HTTP错误：{e}"
            logger.error(f"{err_msg}，响应详情：{response.text if 'response' in locals() else '无'}")
            print(err_msg)
            continue  # 单个父任务失败不终止整体流程
        except Exception as e:
            err_msg = f"查询父任务[{parent_key}]子任务时异常：{str(e)}"
            logger.error(err_msg, exc_info=True)
            print(err_msg)
            continue

    # 7. 将子任务数据写入CSV文件
    if not all_subtasks:
        logger.warning("未查询到任何子任务数据，无需写入CSV")
        print("未查询到任何子任务数据")
    else:
        try:
            # 确保data目录存在
            os.makedirs(os.path.dirname(output_csv_path), exist_ok=True)
            # 覆盖写入CSV
            with open(output_csv_path, 'w', newline='', encoding='utf-8') as f:
                # 定义CSV表头
                headers = ['Issue Key', 'PARENT_KEY', '续费时长（月）', '续费方式', 'ID', '到期时间']
                writer = csv.DictWriter(f, fieldnames=headers)
                writer.writeheader()
                writer.writerows(all_subtasks)

            logger.info(f"子任务数据已写入CSV文件：{output_csv_path}，共{total_subtasks}条记录")
            print(f"子任务数据写入完成，文件路径：{output_csv_path}，共{total_subtasks}条记录")
        except Exception as e:
            err_msg = f"写入子任务CSV文件失败：{str(e)}"
            logger.error(err_msg, exc_info=True)
            print(err_msg)

    # 8. 任务结束
    logger.info("Jira续费子任务数据查询流程结束")
    logger.info("=" * 50 + "\n")


if __name__ == "__main__":
    get_jira_subtasks()