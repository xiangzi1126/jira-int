import requests
from requests.auth import HTTPBasicAuth
import json
import configparser
import os
import csv
from datetime import datetime, timedelta
import logging
# 导入添加评论的函数（引用重命名后的脚本）
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

    # 创建日志目录（若不存在）
    os.makedirs(log_dir, exist_ok=True)

    log_file = os.path.join(log_dir, 'reset_available.log')

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


def get_last_month_bill_filename():
    """获取上月账单文件名，格式为 aliyun_bill_YYYY-MM.csv"""
    today = datetime.today()
    # 计算上月
    first_day_of_current_month = datetime(today.year, today.month, 1)
    last_day_of_last_month = first_day_of_current_month - timedelta(days=1)
    last_month_str = last_day_of_last_month.strftime("%Y-%m")
    return f"aliyun_bill_{last_month_str}.csv"


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

        # 自定义字段ID（更新为实际字段ID）
        FIELD_LAST_MONTH_COST = "customfield_10496"  # 上月花费总额（textfield）

        # 1. 读取Jira账号映射表（包含标签信息）
        jira_account_path = os.path.join(current_dir, '../../jira/data/jira_get_account.csv')
        jira_account_path = os.path.normpath(jira_account_path)
        if not os.path.exists(jira_account_path):
            logger.error(f"Jira账号文件不存在 - {jira_account_path}")
            return

        issue_list = []  # 存储所有Issue信息：[{account, issue_key, tag, total_cost}, ...]
        with open(jira_account_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            required_fields = ['Issue Key', '资源所属账号']
            if not all(field in reader.fieldnames for field in required_fields):
                logger.error("jira_get_account.csv 缺少必要列（Issue Key 或 资源所属账号）")
                return

            # 检查是否有标签列
            has_tag_column = '标签' in reader.fieldnames

            for row in reader:
                resource_account = row['资源所属账号'].strip()
                issue_key = row['Issue Key'].strip()
                tag = row['标签'].strip() if has_tag_column else ''

                if resource_account and issue_key:
                    issue_list.append({
                        'account': resource_account,
                        'issue_key': issue_key,
                        'tag': tag,
                        'total_cost': 0.0  # 初始化消费总额
                    })
        logger.info(f"成功加载 {len(issue_list)} 条Issue映射关系")

        # 2. 读取上月账单数据并计算每个Issue的消费金额
        bill_filename = get_last_month_bill_filename()
        bill_path = os.path.join(current_dir, f'../../jira/data/{bill_filename}')
        bill_path = os.path.normpath(bill_path)

        if not os.path.exists(bill_path):
            logger.error(f"上月账单文件不存在 - {bill_path}")
            return

        with open(bill_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            required_fields = ['资源所属账号', '消费金额']
            if not all(field in reader.fieldnames for field in required_fields):
                logger.error(f"{bill_filename} 缺少必要列（资源所属账号 或 消费金额）")
                return

            # 检查账单是否有标签列
            bill_has_tag_column = '标签' in reader.fieldnames

            for row in reader:
                bill_account = row['资源所属账号'].strip()
                cost_str = row['消费金额'].strip()
                bill_tag = row['标签'].strip() if bill_has_tag_column else ''

                # 尝试转换金额
                try:
                    cost = float(cost_str.replace(',', ''))  # 处理可能的千位分隔符
                except ValueError:
                    logger.warning(f"账号 {bill_account} 的消费金额 '{cost_str}' 格式无效，跳过")
                    continue

                # 遍历所有Issue，匹配则累加金额
                for issue in issue_list:
                    # 条件1：账号必须一致
                    if issue['account'] != bill_account:
                        continue

                    # 条件2：标签匹配规则
                    issue_tag = issue['tag']
                    if issue_tag:  # Issue有标签 → 必须匹配账单标签
                        if not bill_has_tag_column:
                            logger.warning(f"Issue {issue['issue_key']} 有标签，但账单无标签列，跳过该记录")
                            continue
                        if issue_tag not in bill_tag:
                            continue  # 标签不匹配，跳过
                    # （Issue标签为空时，无需检查标签，直接匹配）

                    # 匹配成功，累加金额
                    issue['total_cost'] += cost
                    logger.debug(f"Issue {issue['issue_key']} 累加金额：{cost}，当前总额：{issue['total_cost']}")

        # 3. 批量更新Jira问题并添加评论
        success = 0
        failed = 0
        fail_details = []
        last_month_str = get_last_month_bill_filename().split('_')[2].replace('.csv', '')  # 提取上月年月（YYYY-MM）

        for issue in issue_list:
            issue_key = issue['issue_key']
            total_cost = issue['total_cost']
            total_cost_str = f"{total_cost:.5f}"
            resource_account = issue['account']
            issue_tag = issue['tag']

            logger.info(f"\n准备更新 {issue_key}：")
            logger.info(f"资源所属账号: {resource_account}")
            if issue_tag:
                logger.info(f"匹配标签: {issue_tag}")
            logger.info(f"上月花费总额: {total_cost_str}")

            url = f"https://{jira_domain}/rest/api/3/issue/{issue_key}"
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json"
            }
            payload = {
                "fields": {
                    FIELD_LAST_MONTH_COST: total_cost_str
                }
            }

            try:
                # 更新Jira问题字段
                response = requests.put(
                    url,
                    data=json.dumps(payload),
                    headers=headers,
                    auth=HTTPBasicAuth(username, api_token)
                )
                response.raise_for_status()

                if response.status_code == 204:
                    logger.info(f"成功：更新 {issue_key}（{resource_account}），上月花费总额：{total_cost_str}")

                    # 准备评论内容（包含上月年月和花费总额）
                    comment_body = f"上月（{last_month_str}）花费总额：{total_cost_str}"
                    if issue_tag:
                        comment_body += f"\n匹配标签：{issue_tag}"

                    # 调用添加评论的函数
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
                        error_msg = f"更新成功，但添加评论失败 - {comment_msg}（状态码：{comment_status}）"
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
                if 'response' in locals():
                    error_detail += f"，Jira详情：{response.text}"
                logger.error(f"失败：{issue_key} 异常 - {error_detail}")
                failed += 1
                fail_details.append(f"{issue_key} - {error_detail}")

        # 输出汇总结果
        logger.info("\n" + "=" * 50)
        logger.info(f"批量更新结果汇总：")
        logger.info(f"总处理数：{len(issue_list)}")
        logger.info(f"成功：{success} 条")
        logger.info(f"失败：{failed} 条")
        if fail_details:
            logger.info("\n失败详情：")
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