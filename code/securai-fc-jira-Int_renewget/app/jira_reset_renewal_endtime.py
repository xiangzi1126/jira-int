import requests
from requests.auth import HTTPBasicAuth
import json
import configparser
import os
import csv
from datetime import datetime, timedelta
import logging
from jira_comment import add_jira_comment


def init_logger():
    """初始化日志配置，保存到 ../../jira/log/reset_renewal_endtime.log"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(current_dir, '../../jira/log')
    log_dir = os.path.normpath(log_dir)
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'reset_renewal_endtime.log')
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)


def get_last_month_bill_filename():
    """获取上月续费账单文件名，格式为 aliyun_renewal_bill_YYYY-MM.csv"""
    today = datetime.today()
    first_day_of_current_month = datetime(today.year, today.month, 1)
    last_day_of_last_month = first_day_of_current_month - timedelta(days=1)
    last_month_str = last_day_of_last_month.strftime("%Y-%m")
    return f"aliyun_renewal_bill_{last_month_str}.csv"


def subtract_8_hours_from_time(time_str):
    """
    将时间字符串减去8小时，返回调整后的时间字符串
    支持常见格式：
    - YYYY-MM-DD HH:MM:SS
    - YYYY-MM-DD
    - YYYY-MM-DDTHH:MM:SS（ISO格式）
    - YYYY-MM-DDTHH:MM:SSZ（带Z的UTC ISO格式）
    - YYYY/%m/%d %H:%M:%S
    - YYYY/%m/%d
    """
    if not time_str:
        return ""

    # 新增带Z的ISO格式，优先匹配
    time_formats = [
        "%Y-%m-%dT%H:%M:%SZ",  # 带Z的UTC ISO格式（如2115-07-02T16:00:00Z）
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d"
    ]

    for fmt in time_formats:
        try:
            # 解析时间字符串为datetime对象
            time_obj = datetime.strptime(time_str.strip(), fmt)
            # 减去8小时
            adjusted_time = time_obj - timedelta(hours=8)
            # 按原格式返回（保留Z标识/原格式）
            return adjusted_time.strftime(fmt)
        except ValueError:
            continue

    # 所有格式匹配失败时，返回原字符串并记录警告
    logger = logging.getLogger(__name__)
    logger.warning(f"无法解析时间格式：{time_str}，将使用原始时间")
    return time_str


def batch_update_jira_issues():
    logger = init_logger()
    current_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(current_dir, '../../jira/config/jira_config.ini')
    config_path = os.path.normpath(config_path)

    if not os.path.exists(config_path):
        logger.error(f"配置文件不存在 - {config_path}")
        return

    config = configparser.ConfigParser()
    config.read(config_path, encoding='utf-8')
    logger.info(f"已加载配置文件：{config_path}")

    try:
        jira_domain = config.get('jira', 'domain')
        username = config.get('jira', 'user_name')
        api_token = config.get('jira', 'access_token')
        FIELD_RENEWAL_ENDTIME = "customfield_10500"  # 资源到期时间字段

        # 1. 读取Jira问题映射表
        jira_issue_path = os.path.join(current_dir, '../../jira/data/jira_get_renewal_sbu_issues.csv')
        jira_issue_path = os.path.normpath(jira_issue_path)
        if not os.path.exists(jira_issue_path):
            logger.error(f"Jira问题文件不存在 - {jira_issue_path}")
            return

        issue_list = []
        with open(jira_issue_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            required_fields = ['Issue Key', 'ID']
            if not all(field in reader.fieldnames for field in required_fields):
                logger.error("jira_get_renewal_sbu_issues.csv 缺少必要列（Issue Key 或 ID）")
                return

            for row in reader:
                issue_key = row['Issue Key'].strip()
                resource_id = row['ID'].strip()
                if issue_key:
                    issue_list.append({
                        'issue_key': issue_key,
                        'resource_id': resource_id,
                        'original_endtime': '',  # 新增：存储原始到期时间
                        'adjusted_endtime': ''  # 存储减8小时后的到期时间
                    })
        logger.info(f"成功加载 {len(issue_list)} 条Issue映射关系")

        # 2. 读取上月续费账单数据（修复列名匹配逻辑）
        bill_filename = get_last_month_bill_filename()
        bill_path = os.path.join(current_dir, f'../../jira/data/{bill_filename}')
        bill_path = os.path.normpath(bill_path)

        if not os.path.exists(bill_path):
            logger.error(f"上月续费账单文件不存在 - {bill_path}")
            return

        # 【修改点1】字典值改为存储原始时间和调整后时间的元组 (original, adjusted)
        resource_endtime_map = {}
        with open(bill_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            # 打印实际读取到的列名，方便排查
            logger.info(f"账单文件实际列名：{reader.fieldnames}")

            # 模糊匹配列名（兼容空格/全角字符）
            resource_id_col = None
            endtime_col = None
            for col in reader.fieldnames:
                col_stripped = col.strip()  # 去除首尾空格
                if '资源id' in col_stripped or '资源 ID' in col_stripped:
                    resource_id_col = col
                if '资源到期时间' in col_stripped or '到期时间' in col_stripped:
                    endtime_col = col

            if not resource_id_col:
                logger.error(f"{bill_filename} 缺少「资源id」相关列，实际列名：{reader.fieldnames}")
                return
            if not endtime_col:
                logger.error(f"{bill_filename} 缺少「资源到期时间」相关列，实际列名：{reader.fieldnames}")
                return

            logger.info(f"匹配到资源id列：{resource_id_col}，到期时间列：{endtime_col}")

            for row in reader:
                bill_resource_id = row[resource_id_col].strip()
                original_endtime = row[endtime_col].strip()  # 原始时间

                # 到期时间减去8小时（调整后时间）
                if original_endtime:
                    adjusted_endtime = subtract_8_hours_from_time(original_endtime)
                    logger.debug(f"资源ID {bill_resource_id}：原始时间 {original_endtime} → 调整后 {adjusted_endtime}")
                else:
                    adjusted_endtime = ""

                if bill_resource_id and adjusted_endtime:
                    # 存储原始时间和调整后时间
                    resource_endtime_map[bill_resource_id] = (original_endtime, adjusted_endtime)

        # 【修改点2】匹配Issue时，同时赋值原始时间和调整后时间
        matched_count = 0
        for issue in issue_list:
            resource_id = issue['resource_id']
            if resource_id and resource_id in resource_endtime_map:
                issue['original_endtime'] = resource_endtime_map[resource_id][0]  # 原始时间
                issue['adjusted_endtime'] = resource_endtime_map[resource_id][1]  # 调整后时间
                matched_count += 1
        logger.info(f"成功匹配 {matched_count} 条Issue的到期时间（已减8小时）")

        # 3. 批量更新Jira问题
        success = 0
        failed = 0
        fail_details = []
        last_month_str = get_last_month_bill_filename().split('_')[2].replace('.csv', '')

        for issue in issue_list:
            issue_key = issue['issue_key']
            original_endtime = issue['original_endtime']  # 原始时间
            adjusted_endtime = issue['adjusted_endtime']  # 调整后时间
            resource_id = issue['resource_id']

            if not adjusted_endtime:
                logger.info(f"跳过 {issue_key}：未匹配到到期时间（资源ID：{resource_id}）")
                continue

            logger.info(
                f"\n准备更新 {issue_key}：资源ID={resource_id}，原始到期时间={original_endtime}，调整后到期时间={adjusted_endtime}")
            url = f"https://{jira_domain}/rest/api/3/issue/{issue_key}"
            headers = {"Accept": "application/json", "Content-Type": "application/json"}
            # Jira字段仍更新为调整后时间
            payload = {"fields": {FIELD_RENEWAL_ENDTIME: adjusted_endtime}}

            try:
                response = requests.put(
                    url,
                    data=json.dumps(payload),
                    headers=headers,
                    auth=HTTPBasicAuth(username, api_token)
                )
                response.raise_for_status()

                if response.status_code == 204:
                    logger.info(f"成功更新 {issue_key} 到期时间（调整后：{adjusted_endtime}）")
                    # 【修改点3】评论引用原始时间，说明调整逻辑
                    comment_body = f"上月（{last_month_str}）更新资源到期时间：\n" \
                                   f"原始到期时间：{original_endtime}\n" \
                                   f"资源ID：{resource_id}"

                    comment_success, comment_status, comment_msg = add_jira_comment(
                        jira_domain=jira_domain, issue_key=issue_key,
                        username=username, api_token=api_token, comment_body=comment_body
                    )
                    if comment_success:
                        success += 1
                        logger.info(f"成功为 {issue_key} 添加评论（引用原始时间）")
                    else:
                        error_msg = f"更新成功但评论失败：{comment_msg}（状态码{comment_status}）"
                        logger.error(error_msg)
                        failed += 1
                        fail_details.append(f"{issue_key} - {error_msg}")
                else:
                    error_msg = f"更新失败，状态码{response.status_code}：{response.text}"
                    logger.error(error_msg)
                    failed += 1
                    fail_details.append(f"{issue_key} - {error_msg}")
            except Exception as e:
                error_detail = str(e) + (f"，Jira详情：{response.text}" if 'response' in locals() else "")
                logger.error(f"{issue_key} 异常：{error_detail}")
                failed += 1
                fail_details.append(f"{issue_key} - {error_detail}")

        # 汇总日志
        logger.info("\n" + "=" * 50)
        logger.info(f"批量更新结果：总Issue={len(issue_list)}，匹配到期时间={matched_count}，成功={success}，失败={failed}")
        if fail_details:
            logger.info("失败详情：")
            for d in fail_details:
                logger.info(f"- {d}")

    except configparser.NoSectionError:
        logger.error("配置文件缺少[jira]段落")
    except configparser.NoOptionError as e:
        logger.error(f"配置文件缺少参数：{e}")
    except Exception as e:
        logger.error(f"执行错误：{str(e)}")


if __name__ == "__main__":
    batch_update_jira_issues()