import requests
from requests.auth import HTTPBasicAuth
import configparser
import os
import csv
import logging
from datetime import datetime


# 初始化日志配置
def init_logger():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.normpath(os.path.join(current_dir, '../../jira/log'))
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'request_type_query.log')

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


def query_jira_request_types():
    logger.info("=" * 50)
    logger.info(f"开始查询所有Jira Service Desk的Request Type（时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}）")

    # 1. 读取配置文件
    current_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.normpath(os.path.join(current_dir, '../../jira/config/jira_config.ini'))

    if not os.path.exists(config_path):
        logger.error(f"配置文件不存在 - {config_path}")
        return

    config = configparser.ConfigParser()
    try:
        config.read(config_path, encoding='utf-8')
        jira_domain = config.get('jira', 'domain')
        user_name = config.get('jira', 'user_name')
        access_token = config.get('jira', 'access_token')
        project_key_str = config.get('jira', 'project_key')
        project_keys = {key.strip() for key in project_key_str.split(',') if key.strip()}
    except Exception as e:
        logger.error(f"读取配置参数失败：{str(e)}")
        return

    # 2. 第一步：分页查询所有 Service Desk 项目
    logger.info("\n===== 开始分页查询 Service Desk 项目 =====")
    service_desk_list = []
    headers = {"Accept": "application/json"}
    auth = HTTPBasicAuth(user_name, access_token)

    start = 0
    limit = 50
    is_last_page = False

    while not is_last_page:
        try:
            # 在 URL 中加入分页参数 start 和 limit
            service_desk_url = f"https://{jira_domain}/rest/servicedeskapi/servicedesk?start={start}&limit={limit}"
            response = requests.get(service_desk_url, headers=headers, auth=auth)
            response.raise_for_status()
            data = response.json()

            values = data.get('values', [])
            for desk in values:
                if desk['projectKey'] in project_keys:
                    service_desk_list.append({
                        'projectName': desk['projectName'],
                        'projectKey': desk['projectKey'],
                        'serviceDeskId': desk['id']
                    })
                    logger.info(f"命中匹配项目：{desk['projectKey']} (ID: {desk['id']})")

            # 检查是否还有下一页
            is_last_page = data.get('isLastPage', True)
            start += len(values)
            if not is_last_page:
                logger.info(f"已处理 {start} 条项目，准备加载下一页...")

        except Exception as e:
            logger.error(f"查询项目列表时出错：{str(e)}")
            break

    if not service_desk_list:
        logger.warning(f"未找到指定的 Service Desk 项目：{project_keys}")
        return

    # 3. 第二步：查询每个项目的 Request Type (同样处理分页)
    logger.info("\n===== 开始查询 Request Type (支持分页) =====")
    all_request_type_data = []

    for desk in service_desk_list:
        p_name = desk['projectName']
        p_key = desk['projectKey']
        sd_id = desk['serviceDeskId']

        logger.info(f"--- 正在提取项目「{p_name}」的类型 ---")

        rt_start = 0
        rt_is_last = False

        while not rt_is_last:
            rt_url = f"https://{jira_domain}/rest/servicedeskapi/servicedesk/{sd_id}/requesttype?start={rt_start}&limit={limit}"
            resp = requests.get(rt_url, headers=headers, auth=auth)
            resp.raise_for_status()
            rt_data = resp.json()

            rt_values = rt_data.get('values', [])
            if not rt_values and rt_start == 0:
                all_request_type_data.append([p_name, p_key, sd_id, "无", "无"])
                break

            for rt in rt_values:
                all_request_type_data.append([p_name, p_key, sd_id, rt['name'], rt['id']])
                logger.info(f"  [Type] {rt['name']} (ID: {rt['id']})")

            rt_is_last = rt_data.get('isLastPage', True)
            rt_start += len(rt_values)

    # 4. 写入 CSV
    data_dir = os.path.normpath(os.path.join(current_dir, '../../jira/data'))
    os.makedirs(data_dir, exist_ok=True)
    csv_file = os.path.join(data_dir, 'jira_request_type.csv')

    with open(csv_file, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(['项目名称', '项目键', 'ServiceDeskID', 'Request Type名称', 'Request TypeID'])
        writer.writerows(all_request_type_data)

    logger.info(f"\n查询完成！共抓取 {len(all_request_type_data)} 条记录。")
    logger.info(f"保存路径：{csv_file}")
    logger.info("=" * 50 + "\n")


if __name__ == "__main__":
    query_jira_request_types()