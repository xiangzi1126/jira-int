import requests
from requests.auth import HTTPBasicAuth
import json
import configparser
import os
import csv
from datetime import datetime
import logging
# 导入添加评论的函数
from jira_comment import add_jira_comment


def clean_amount(value):
    """清洗金额格式，保留原始格式（仅去除首尾空格）"""
    if isinstance(value, str):
        return value.strip()
    return str(value)


def init_logger():
    """初始化日志配置，保存到 ../../jira/log/reset_available.log"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(current_dir, '../../jira/log')
    log_dir = os.path.normpath(log_dir)

    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'reset_available.log')

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)


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

        # 自定义字段ID
        FIELD_QUERY_TIME = "customfield_10497"
        FIELD_AVAILABLE_CASH = "customfield_10498"
        FIELD_AVAILABLE_AMOUNT = "customfield_10499"
        FIELD_CURRENCY = "customfield_10490"

        # ==============================================================================
        # 修改点 1：使用 List 而不是 Dict 读取 Jira 账号表，确保每一行都被处理
        # ==============================================================================
        jira_account_path = os.path.join(current_dir, '../../jira/data/jira_get_account.csv')
        jira_account_path = os.path.normpath(jira_account_path)
        if not os.path.exists(jira_account_path):
            logger.error(f"Jira账号文件不存在 - {jira_account_path}")
            return

        jira_account_rows = []  # 改用列表存储
        with open(jira_account_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            if 'Issue Key' not in reader.fieldnames or '资源所属账号' not in reader.fieldnames:
                logger.error("jira_get_account.csv 缺少必要列（Issue Key 或 资源所属账号）")
                return
            for row in reader:
                resource_account = row['资源所属账号'].strip()
                issue_key = row['Issue Key'].strip()
                if resource_account and issue_key:
                    # 将每一行作为一个字典存入列表
                    jira_account_rows.append({
                        'resource_account': resource_account,
                        'issue_key': issue_key
                    })
        logger.info(f"成功加载 {len(jira_account_rows)} 行账号待处理记录")

        # 2. 读取当日余额数据（保持不变，用于快速查找）
        current_date = datetime.now().strftime('%Y%m%d')
        balance_file_path = os.path.join(current_dir, f'../../jira/data/balance.csv')
        balance_file_path = os.path.normpath(balance_file_path)
        if not os.path.exists(balance_file_path):
            logger.error(f"余额文件不存在 - {balance_file_path}")
            return

        balance_data = {}
        with open(balance_file_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            required_columns = ['role_session_name', 'query_time', 'available_cash_amount', 'available_amount',
                                'currency']
            for col in required_columns:
                if col not in reader.fieldnames:
                    logger.error(f"余额文件缺少必要列 - {col}")
                    return

            for row in reader:
                role_session = row['role_session_name'].strip()
                original_query_time = row['query_time'].strip()
                try:
                    query_time = datetime.strptime(original_query_time, '%Y-%m-%d %H:%M:%S').strftime(
                        '%Y-%m-%dT%H:%M:%S.000+0800')
                except ValueError:
                    logger.warning(f"{role_session} 的时间格式错误，跳过")
                    continue

                # 数据清洗逻辑
                if role_session and query_time:
                    balance_data[role_session] = {
                        'query_time': query_time,
                        'available_cash_amount': clean_amount(row['available_cash_amount']),
                        'available_amount': clean_amount(row['available_amount']),
                        'currency': clean_amount(row['currency'])
                    }
        logger.info(f"成功加载 {len(balance_data)} 条余额查找数据")

        # ==============================================================================
        # 修改点 2：遍历列表进行更新，实现“一行记录对应一次更新”
        # ==============================================================================
        success = 0
        failed = 0
        fail_details = []

        for row_data in jira_account_rows:
            resource_account = row_data['resource_account']
            issue_key = row_data['issue_key']

            # 判断条件：用 csv 行里的账号去 balance_data 字典里查找
            if resource_account not in balance_data:
                logger.warning(f"Issue: {issue_key} (账号: {resource_account}) - 无匹配的余额数据，跳过")
                failed += 1
                fail_details.append(f"{issue_key} ({resource_account}) - 无匹配余额数据")
                continue

            # 获取匹配到的余额数据
            balance_record = balance_data[resource_account]

            # --- 以下逻辑保持不变 ---
            query_time = balance_record['query_time']
            available_cash_amount = balance_record['available_cash_amount']
            available_amount = balance_record['available_amount']
            currency = balance_record['currency']

            logger.info(f"\n准备更新 {issue_key} (账号: {resource_account})")

            url = f"https://{jira_domain}/rest/api/3/issue/{issue_key}"
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json"
            }
            payload = {
                "fields": {
                    FIELD_QUERY_TIME: query_time,
                    FIELD_AVAILABLE_CASH: available_cash_amount,
                    FIELD_AVAILABLE_AMOUNT: available_amount,
                    FIELD_CURRENCY: currency
                }
            }

            try:
                response = requests.put(
                    url,
                    data=json.dumps(payload),
                    headers=headers,
                    auth=HTTPBasicAuth(username, api_token)
                )
                response.raise_for_status()

                if response.status_code == 204:
                    logger.info(f"成功：更新 {issue_key}")

                    comment_body = (
                        f"账号：{resource_account}\n"  # 在评论里也加上账号名以便区分
                        f"余额核查时间：{query_time}\n"
                        f"可用现金余额：{available_cash_amount}\n"
                        f"可用余额：{available_amount}\n"
                        f"币种：{currency}"
                    )

                    comment_success, comment_status, comment_msg = add_jira_comment(
                        jira_domain=jira_domain,
                        issue_key=issue_key,
                        username=username,
                        api_token=api_token,
                        comment_body=comment_body
                    )

                    if comment_success:
                        logger.info(f"成功：为 {issue_key} 添加评论")
                        success += 1
                    else:
                        error_msg = f"更新成功但评论失败: {comment_msg}"
                        logger.error(f"警告：{issue_key} - {error_msg}")
                        failed += 1
                        fail_details.append(f"{issue_key} - {error_msg}")
                else:
                    error_msg = f"状态码 {response.status_code}，详情：{response.text}"
                    logger.error(f"失败：{issue_key} - {error_msg}")
                    failed += 1
                    fail_details.append(f"{issue_key} - {error_msg}")

            except Exception as e:
                error_detail = str(e)
                logger.error(f"失败：{issue_key} 异常 - {error_detail}")
                failed += 1
                fail_details.append(f"{issue_key} - {error_detail}")

        # 输出汇总
        logger.info("\n" + "=" * 50)
        logger.info(f"处理完成。总行数：{len(jira_account_rows)}")
        logger.info(f"成功：{success}")
        logger.info(f"失败/跳过：{failed}")
        if fail_details:
            logger.info("\n失败详情摘要：")
            for detail in fail_details:
                logger.info(f"- {detail}")

    except configparser.NoSectionError:
        logger.error("配置文件缺少 [jira] 段落")
    except configparser.NoOptionError as e:
        logger.error(f"配置文件缺少参数 - {e}")
    except Exception as e:
        logger.error(f"执行错误：{str(e)}")


if __name__ == "__main__":
    batch_update_jira_issues()