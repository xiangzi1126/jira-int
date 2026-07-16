import requests
from requests.auth import HTTPBasicAuth
import json
import configparser
import os
import csv
import logging
from datetime import datetime


def init_logger():
    """初始化日志，追加输出到 ../../jira/log/renewal.log 和控制台"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.normpath(os.path.join(current_dir, '../../jira/log'))
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'renewal.log')

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file, mode='a', encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)


def batch_update_jira_no_renewal_status():
    logger = init_logger()
    logger.info("=" * 50)
    logger.info(f"开始执行Jira续费完成状态批量更新（时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}）")

    # 1. 定义文件路径
    current_dir = os.path.dirname(os.path.abspath(__file__))
    jira_config_path = os.path.normpath(os.path.join(current_dir, '../../jira/config/jira_config.ini'))
    input_csv_path = os.path.normpath(os.path.join(current_dir, '../../jira/data/jira_get_need_renewal.csv'))

    # 2. 校验输入CSV文件是否存在
    if not os.path.exists(input_csv_path):
        err_msg = f"输入CSV文件不存在 - {input_csv_path}"
        logger.error(err_msg)
        print(err_msg)
        return

    # 3. 读取CSV并筛选续费状态为true的Issue
    target_issues = []
    try:
        with open(input_csv_path, 'r', encoding='utf-8', newline='') as f:
            reader = csv.DictReader(f)
            # 校验必要列
            required_fields = ['Issue Key', '续费状态']
            if not all(field in reader.fieldnames for field in required_fields):
                err_msg = f"输入CSV文件缺少必要列（需包含Issue Key、续费状态） - {input_csv_path}"
                logger.error(err_msg)
                print(err_msg)
                return

            for row in reader:
                issue_key = row['Issue Key'].strip()
                renewal_status = row['续费状态'].strip().lower()

                # 筛选续费状态为true的行（兼容字符串"true"或布尔值）
                if issue_key and renewal_status == 'true':
                    target_issues.append({
                        'issue_key': issue_key,
                        'parent_key': row.get('PARENT_KEY', '').strip()
                    })

        if not target_issues:
            logger.info("未找到续费状态为true的Jira工作项，无需执行状态修改")
            return
        logger.info(f"共筛选出 {len(target_issues)} 个续费状态为true的Jira工作项")

    except Exception as e:
        err_msg = f"读取输入CSV文件失败：{str(e)}"
        logger.error(err_msg, exc_info=True)
        print(err_msg)
        return

    # 4. 读取Jira配置文件
    if not os.path.exists(jira_config_path):
        err_msg = f"Jira配置文件不存在 - {jira_config_path}"
        logger.error(err_msg)
        print(err_msg)
        return

    config = configparser.ConfigParser()
    try:
        config.read(jira_config_path, encoding='utf-8')
        logger.info(f"已加载Jira配置文件：{jira_config_path}")
    except Exception as e:
        err_msg = f"读取Jira配置文件失败：{str(e)}"
        logger.error(err_msg, exc_info=True)
        print(err_msg)
        return

    # 5. 初始化Jira API认证信息
    try:
        jira_domain = config.get('jira', 'domain')
        user_name = config.get('jira', 'user_name')
        access_token = config.get('jira', 'access_token')
        jira_url = f"https://{jira_domain}"
        auth = HTTPBasicAuth(user_name, access_token)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
        logger.info(f"成功读取Jira配置 - 域名：{jira_domain}，用户名：{user_name}")
    except (configparser.NoSectionError, configparser.NoOptionError) as e:
        err_msg = f"Jira配置文件解析错误：{str(e)}"
        logger.error(err_msg)
        print(err_msg)
        return

    # 6. 批量修改Jira工作项状态为“续费完成”
    success_count = 0
    fail_count = 0
    fail_details = []
    target_status_name = "续费完成"

    for issue in target_issues:
        issue_key = issue['issue_key']
        parent_key = issue['parent_key']
        logger.info(f"\n开始处理 Issue：{issue_key}（父任务：{parent_key if parent_key else '无'}）")

        try:
            # 6.1 获取该Issue的可用状态转换
            transition_url = f"{jira_url}/rest/api/3/issue/{issue_key}/transitions"
            resp = requests.get(transition_url, headers=headers, auth=auth)
            resp.raise_for_status()
            transition_data = resp.json()

            # 6.2 查找目标状态转换ID（到“续费完成”的transition）
            target_transition_id = None
            for t in transition_data.get("transitions", []):
                if t.get('to', {}).get('name') == target_status_name:
                    target_transition_id = t['id']
                    logger.info(
                        f"Issue {issue_key} 找到到「{target_status_name}」的转换ID：{target_transition_id}（转换名称：{t['name']}）")
                    break

            if not target_transition_id:
                error_msg = f"当前状态下没有到「{target_status_name}」的转换，请确认工作流配置"
                logger.error(f"Issue {issue_key} - {error_msg}")
                fail_count += 1
                fail_details.append(f"{issue_key} - {error_msg}")
                continue

            # 6.3 执行状态转换
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
                logger.info(f"Issue {issue_key} 状态已成功修改为「{target_status_name}」")
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
            logger.error(f"Issue {issue_key} - {error_detail}", exc_info=True)
            fail_count += 1
            fail_details.append(f"{issue_key} - {error_detail}")

    # 7. 输出汇总结果
    logger.info("\n" + "=" * 60)
    logger.info(f"Jira状态修改结果汇总：")
    logger.info(f"筛选出待处理Issue数：{len(target_issues)}")
    logger.info(f"成功修改状态数：{success_count}")
    logger.info(f"修改失败数：{fail_count}")

    if fail_details:
        logger.info("\n失败详情：")
        for detail in fail_details:
            logger.info(f"- {detail}")
    logger.info("=" * 50 + "\n")


if __name__ == "__main__":
    batch_update_jira_no_renewal_status()