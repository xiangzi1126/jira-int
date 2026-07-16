import requests
from requests.auth import HTTPBasicAuth
import json
import configparser
import os
import csv
from datetime import datetime, timedelta
import logging


def init_logger():
    """初始化日志配置，保存到 ../../jira/log/renewal.log"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(current_dir, '../../jira/log')
    log_dir = os.path.normpath(log_dir)

    # 创建日志目录（若不存在）
    os.makedirs(log_dir, exist_ok=True)

    log_file = os.path.join(log_dir, 'renewal.log')

    # 配置日志格式
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),  # 写入文件
            logging.StreamHandler()  # 同时输出到控制台
        ]
    )
    return logging.getLogger(__name__)


def parse_endtime_to_month(endtime_str):
    """
    解析到期时间字符串，返回(年, 月)元组；解析失败返回None
    支持格式：
    - YYYY-MM-DDTHH:MM:SS.000+0800（带毫秒+时区偏移）
    - YYYY-MM-DDTHH:MM:SSZ（带Z的UTC格式）
    - YYYY-MM-DD HH:MM:SS（带时分秒）
    - YYYY-MM-DD（仅日期）
    - 其他常见时间格式
    """
    if not endtime_str:
        return None

    # 【修复点】新增带毫秒+时区的格式，优先匹配
    time_formats = [
        "%Y-%m-%dT%H:%M:%S.%f%z",  # 带毫秒+时区（如2026-04-26T16:00:00.000+0800）
        "%Y-%m-%dT%H:%M:%SZ",  # 带Z的UTC格式
        "%Y-%m-%d %H:%M:%S",  # 带时分秒
        "%Y-%m-%dT%H:%M:%S",  # ISO格式（无毫秒/时区）
        "%Y-%m-%d",  # 仅日期
        "%Y/%m/%d %H:%M:%S",  # 斜杠分隔日期+时分秒
        "%Y/%m/%d"  # 斜杠分隔仅日期
    ]

    for fmt in time_formats:
        try:
            time_obj = datetime.strptime(endtime_str.strip(), fmt)
            return (time_obj.year, time_obj.month)
        except ValueError:
            continue

    # 所有格式匹配失败
    logger = logging.getLogger(__name__)
    logger.warning(f"无法解析到期时间格式：{endtime_str}")
    return None


def batch_update_jira_status():
    logger = init_logger()
    current_dir = os.path.dirname(os.path.abspath(__file__))

    # 1. 读取Jira配置文件
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
        jira_url = f"https://{jira_domain}"
        auth = HTTPBasicAuth(username, api_token)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json"
        }

        # 2. 读取Jira工作项CSV文件
        jira_issue_path = os.path.join(current_dir, '../../jira/data/jira_get_renewal_sbu_issues.csv')
        jira_issue_path = os.path.normpath(jira_issue_path)
        if not os.path.exists(jira_issue_path):
            logger.error(f"Jira工作项文件不存在 - {jira_issue_path}")
            return

        # 存储符合条件的Issue：[{issue_key, endtime, endtime_month}, ...]
        target_issues = []
        # 获取当前年月（用于判断是否为本月）
        current_year, current_month = datetime.today().year, datetime.today().month
        logger.info(f"当前年月：{current_year}年{current_month}月，筛选到期时间为本月的Issue")

        with open(jira_issue_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            # 校验必要列
            required_fields = ['Issue Key', '到期时间']
            if not all(field in reader.fieldnames for field in required_fields):
                logger.error("jira_get_renewal_sbu_issues.csv 缺少必要列（Issue Key 或 到期时间）")
                return

            for row in reader:
                issue_key = row['Issue Key'].strip()
                endtime_str = row['到期时间'].strip()

                # 跳过空的Issue Key或到期时间
                if not issue_key or not endtime_str:
                    continue

                # 解析到期时间的年月
                endtime_month = parse_endtime_to_month(endtime_str)
                if not endtime_month:
                    logger.warning(f"Issue {issue_key} 到期时间格式无效，跳过")
                    continue

                endtime_year, endtime_mon = endtime_month
                # 判断是否为本月
                if endtime_year == current_year and endtime_mon == current_month:
                    target_issues.append({
                        'issue_key': issue_key,
                        'endtime': endtime_str,
                        'endtime_month': f"{endtime_year}-{endtime_mon:02d}"
                    })
                    logger.debug(f"Issue {issue_key} 到期时间{endtime_str}为本月，加入待处理列表")

        if not target_issues:
            logger.info(f"未找到到期时间为{current_year}年{current_month}月的Jira工作项，无需执行状态修改")
            return
        logger.info(f"共筛选出 {len(target_issues)} 个到期时间为本月的Jira工作项")

        # 3. 批量修改Jira工作项状态为“等待分配”
        success_count = 0
        fail_count = 0
        fail_details = []

        for issue in target_issues:
            issue_key = issue['issue_key']
            logger.info(f"\n开始处理 Issue：{issue_key}（到期时间：{issue['endtime']}）")

            try:
                # 3.1 获取该Issue的可用状态转换
                transition_url = f"{jira_url}/rest/api/3/issue/{issue_key}/transitions"
                resp = requests.get(transition_url, headers=headers, auth=auth)
                resp.raise_for_status()
                transition_data = resp.json()

                # ===================== 硬编码：直接使用 transition id = 4 =====================
                target_transition_id = "4"
                logger.info(f"Issue {issue_key} 使用硬编码转换ID：{target_transition_id}")

                # 3.3 执行状态转换
                payload = {
                    "transition": {
                        "id": target_transition_id
                    }
                }
                resp2 = requests.post(
                    transition_url,
                    headers=headers,
                    auth=auth,
                    data=json.dumps(payload)
                )
                resp2.raise_for_status()

                # 状态码204表示成功（无返回内容）
                if resp2.status_code == 204:
                    logger.info(f"Issue {issue_key} 状态已成功修改为「等待分配」")
                    success_count += 1
                else:
                    error_msg = f"状态修改失败，状态码：{resp2.status_code}，详情：{resp2.text}"
                    logger.error(f"Issue {issue_key} - {error_msg}")
                    fail_count += 1
                    fail_details.append(f"{issue_key} - {error_msg}")

            except requests.exceptions.HTTPError as e:
                error_detail = f"HTTP请求错误：{str(e)}"
                if 'resp2' in locals() and resp2.text:
                    error_detail += f"，Jira详情：{resp2.text}"
                logger.error(f"Issue {issue_key} - {error_detail}")
                fail_count += 1
                fail_details.append(f"{issue_key} - {error_detail}")
            except Exception as e:
                error_detail = f"执行异常：{str(e)}"
                logger.error(f"Issue {issue_key} - {error_detail}")
                fail_count += 1
                fail_details.append(f"{issue_key} - {error_detail}")

        # 4. 输出汇总结果
        logger.info("\n" + "=" * 60)
        logger.info(f"Jira状态修改结果汇总：")
        logger.info(f"筛选出待处理Issue数：{len(target_issues)}")
        logger.info(f"成功修改状态数：{success_count}")
        logger.info(f"修改失败数：{fail_count}")

        if fail_details:
            logger.info("\n失败详情：")
            for detail in fail_details:
                logger.info(f"- {detail}")

    except configparser.NoSectionError:
        logger.error("配置文件缺少 [jira] 段落")
    except configparser.NoOptionError as e:
        logger.error(f"配置文件缺少参数 - {e}")
    except Exception as e:
        logger.error(f"脚本执行错误：{str(e)}")


if __name__ == "__main__":
    batch_update_jira_status()