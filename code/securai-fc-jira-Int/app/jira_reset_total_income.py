import requests
from requests.auth import HTTPBasicAuth
import json
import configparser
import os
import csv
from datetime import datetime, timedelta
import logging
# 导入添加评论的函数
from jira_comment import add_jira_comment


def clean_amount(value):
    """清洗金额格式，去除逗号并转为float"""
    if isinstance(value, str):
        # 去除首尾空格和千分位逗号
        cleaned = value.strip().replace(',', '')
        try:
            return float(cleaned)
        except ValueError:
            return 0.0
    return float(value) if value else 0.0


def init_logger():
    """初始化日志配置"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(current_dir, '../../jira/log')
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


def get_last_month_income_filename():
    """获取上月充值记录文件名"""
    today = datetime.today()
    first_day_of_current_month = datetime(today.year, today.month, 1)
    last_day_of_last_month = first_day_of_current_month - timedelta(days=1)
    last_month_str = last_day_of_last_month.strftime("%Y-%m")
    return f"aliyun_income_{last_month_str}.csv", last_month_str


def load_income_data(income_path, logger):
    """
    读取充值文件，返回一个字典：{ '账号名称': 总金额 }
    """
    if not os.path.exists(income_path):
        logger.error(f"充值记录文件不存在 - {income_path}")
        return None

    income_map = {}
    logger.info(f"正在读取充值数据: {os.path.basename(income_path)} ...")

    with open(income_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        if '资源所属账号' not in reader.fieldnames or '消费金额' not in reader.fieldnames:
            logger.error("充值文件缺少必要列（资源所属账号 或 消费金额）")
            return None

        for row in reader:
            account = row['资源所属账号'].strip()
            amount_str = row['消费金额']
            amount = clean_amount(amount_str)

            # 累加金额（防止同一账号在流水中出现多次）
            if account in income_map:
                income_map[account] += amount
            else:
                income_map[account] = amount

    logger.info(f"充值数据加载完成，共包含 {len(income_map)} 个账号的记录")
    return income_map


def batch_update_jira_issues():
    logger = init_logger()
    current_dir = os.path.dirname(os.path.abspath(__file__))

    # 1. 加载配置
    config_path = os.path.join(current_dir, '../../jira/config/jira_config.ini')
    if not os.path.exists(config_path):
        logger.error(f"配置文件不存在 - {config_path}")
        return

    config = configparser.ConfigParser()
    config.read(config_path, encoding='utf-8')

    try:
        jira_domain = config.get('jira', 'domain')
        username = config.get('jira', 'user_name')
        api_token = config.get('jira', 'access_token')
        FIELD_LAST_MONTH_RECHARGE = "customfield_10495"  # 上月充值总额字段ID

        # 2. 准备充值数据 (作为查找字典)
        income_filename, last_month_str = get_last_month_income_filename()
        income_path = os.path.join(current_dir, f'../../jira/data/{income_filename}')

        # 获取所有账号的充值汇总字典 {account: total_amount}
        income_data_map = load_income_data(income_path, logger)
        if income_data_map is None:
            return  # 数据加载失败，终止程序

        # 3. 遍历 jira_get_account.csv，逐行处理
        jira_account_path = os.path.join(current_dir, '../../jira/data/jira_get_account.csv')
        if not os.path.exists(jira_account_path):
            logger.error(f"Jira账号映射文件不存在 - {jira_account_path}")
            return

        success_count = 0
        failed_count = 0

        logger.info(f"开始处理 Jira 账号映射文件: {jira_account_path}")

        with open(jira_account_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)

            # 检查列名
            if 'Issue Key' not in reader.fieldnames or '资源所属账号' not in reader.fieldnames:
                logger.error("jira_get_account.csv 缺少必要列（Issue Key 或 资源所属账号）")
                return

            # --- 循环开始：一行对应一个 Issue ---
            for row in reader:
                target_account = row['资源所属账号'].strip()
                issue_key = row['Issue Key'].strip()

                if not target_account or not issue_key:
                    logger.warning(f"跳过无效行: Account='{target_account}', Key='{issue_key}'")
                    continue

                # 在充值数据字典中查找该账号的金额（如果没找到，默认为 0.0）
                total_recharge = income_data_map.get(target_account, 0.0)
                total_recharge_str = f"{total_recharge:.2f}"

                logger.info("-" * 30)
                logger.info(f"正在处理 Issue: {issue_key}")
                logger.info(f"  - 资源账号: {target_account}")
                logger.info(f"  - 匹配金额: {total_recharge_str}")

                # 准备更新 Jira
                url = f"https://{jira_domain}/rest/api/3/issue/{issue_key}"
                headers = {
                    "Accept": "application/json",
                    "Content-Type": "application/json"
                }
                payload = {
                    "fields": {
                        FIELD_LAST_MONTH_RECHARGE: total_recharge_str
                    }
                }

                try:
                    # A. 更新字段
                    response = requests.put(
                        url,
                        data=json.dumps(payload),
                        headers=headers,
                        auth=HTTPBasicAuth(username, api_token)
                    )

                    if response.status_code == 204:
                        logger.info(f"  - 字段更新成功")

                        # B. 添加评论
                        comment_body = f"上月（{last_month_str}）充值总额：{total_recharge_str}"
                        c_success, c_status, c_msg = add_jira_comment(
                            jira_domain=jira_domain,
                            issue_key=issue_key,
                            username=username,
                            api_token=api_token,
                            comment_body=comment_body
                        )

                        if c_success:
                            logger.info(f"  - 评论添加成功")
                            success_count += 1
                        else:
                            logger.error(f"  - 字段更新成功但评论失败: {c_msg}")
                            failed_count += 1
                    else:
                        logger.error(f"  - 更新失败: HTTP {response.status_code} - {response.text}")
                        failed_count += 1

                except Exception as e:
                    logger.error(f"  - 处理异常: {str(e)}")
                    failed_count += 1
            # --- 循环结束 ---

        logger.info("=" * 30)
        logger.info(f"处理完成。成功: {success_count}, 失败: {failed_count}")

    except Exception as e:
        logger.error(f"脚本执行发生未捕获异常: {str(e)}")


if __name__ == "__main__":
    batch_update_jira_issues()