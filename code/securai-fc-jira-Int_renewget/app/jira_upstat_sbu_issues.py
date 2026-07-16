import requests
from requests.auth import HTTPBasicAuth
import json
import configparser
import os
import logging
import csv  # 新增：用于读取CSV
from datetime import datetime


def init_logger():
    """初始化日志配置，保存到 ../../jira/log/renewal.log"""
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


def load_jira_config(logger):
    """加载Jira配置文件并返回认证信息"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.normpath(os.path.join(current_dir, '../../jira/config/jira_config.ini'))

    if not os.path.exists(config_path):
        logger.error(f"Jira配置文件不存在：{config_path}")
        raise FileNotFoundError(f"Jira配置文件不存在：{config_path}")

    config = configparser.ConfigParser()
    config.read(config_path, encoding='utf-8')
    logger.info(f"已加载配置文件：{config_path}")

    try:
        jira_domain = config.get('jira', 'domain')
        username = config.get('jira', 'user_name')
        api_token = config.get('jira', 'access_token')
        return {
            'domain': jira_domain,
            'username': username,
            'token': api_token,
            'url': f"https://{jira_domain}"
        }
    except (configparser.NoSectionError, configparser.NoOptionError) as e:
        logger.error(f"配置文件参数缺失：{e}")
        raise


def load_parent_issue_keys(logger):
    """从CSV文件加载父项Issue Key列表"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    csv_path = os.path.normpath(os.path.join(current_dir, '../../jira/data/jira_get_renewal_issues.csv'))

    # 检查CSV文件是否存在
    if not os.path.exists(csv_path):
        logger.error(f"父项Issue Key CSV文件不存在：{csv_path}")
        raise FileNotFoundError(f"父项Issue Key CSV文件不存在：{csv_path}")

    parent_keys = []
    try:
        with open(csv_path, mode='r', encoding='utf-8') as f:
            reader = csv.DictReader(f)

            # 检查CSV是否包含必要列
            if 'Issue Key' not in reader.fieldnames:
                logger.error(f"CSV文件缺少必要列：'Issue Key'，现有列：{reader.fieldnames}")
                raise ValueError(f"CSV文件缺少必要列：'Issue Key'")

            # 提取非空的Issue Key
            for row in reader:
                issue_key = row.get('Issue Key', '').strip()
                if issue_key:
                    parent_keys.append(issue_key)

        logger.info(f"已从CSV加载 {len(parent_keys)} 个父项Issue Key")
        if not parent_keys:
            logger.warning("CSV文件中未找到有效的父项Issue Key")

        return parent_keys

    except Exception as e:
        logger.error(f"加载父项Issue Key异常：{e}")
        raise


def search_issues_by_jql(jira_config, jql, fields, logger):
    """通过JQL查询目标工单"""
    max_results = 1000
    url = f"{jira_config['url']}/rest/api/3/search/jql"
    auth = HTTPBasicAuth(jira_config['username'], jira_config['token'])
    headers = {"Accept": "application/json"}

    logger.info(f"生成查询JQL：{jql}")
    logger.info(f"查询字段：{fields}，单次最大结果数：{max_results}")

    try:
        response = requests.get(
            url,
            params={"jql": jql, "fields": fields, "maxResults": max_results},
            auth=auth,
            headers=headers,
            timeout=30
        )
        response.raise_for_status()
        data = response.json()
        issues = data.get("issues", [])
        logger.info(f"API请求成功，共找到 {len(issues)} 个目标工单")
        return issues
    except requests.exceptions.HTTPError as e:
        logger.error(f"HTTP请求错误：{e}，响应详情：{response.text if 'response' in locals() else '无'}")
        raise
    except Exception as e:
        logger.error(f"查询目标工单异常：{e}")
        raise


def transition_issue_status(jira_config, issue_key, target_status, logger):
    """将单个工单转换为目标状态"""
    auth = HTTPBasicAuth(jira_config['username'], jira_config['token'])
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    transition_url = f"{jira_config['url']}/rest/api/3/issue/{issue_key}/transitions"

    try:
        resp = requests.get(transition_url, auth=auth, headers=headers, timeout=30)
        resp.raise_for_status()
        transition_data = resp.json()

        target_transition_id = None
        for t in transition_data.get("transitions", []):
            if t.get('to', {}).get('name') == target_status:
                target_transition_id = t['id']
                logger.info(f"Issue {issue_key} 找到到「{target_status}」的转换ID：{target_transition_id}")
                break

        if not target_transition_id:
            error_msg = f"当前状态下没有到「{target_status}」的转换，请确认工作流配置"
            logger.error(f"Issue {issue_key} - {error_msg}")
            return False

        payload = {"transition": {"id": target_transition_id}}
        resp2 = requests.post(
            transition_url,
            auth=auth,
            headers=headers,
            data=json.dumps(payload),
            timeout=30
        )
        resp2.raise_for_status()

        if resp2.status_code == 204:
            logger.info(f"Issue {issue_key} 状态已成功修改为「{target_status}」")
            return True
        else:
            error_msg = f"状态修改失败，状态码：{resp2.status_code}，详情：{resp2.text}"
            logger.error(f"Issue {issue_key} - {error_msg}")
            return False

    except requests.exceptions.HTTPError as e:
        error_detail = f"HTTP请求错误：{str(e)}"
        if 'resp2' in locals() and resp2.text:
            error_detail += f"，Jira详情：{resp2.text}"
        logger.error(f"Issue {issue_key} - {error_detail}")
        return False
    except Exception as e:
        error_detail = f"执行异常：{str(e)}"
        logger.error(f"Issue {issue_key} - {error_detail}")
        return False


def process_issues(jira_config, jql, task_name, target_status, logger):
    """处理一批特定JQL查询出的工单"""
    logger.info("\n" + "=" * 60)
    logger.info(f"开始执行任务：{task_name}")
    logger.info("=" * 60)

    success_count = 0
    fail_count = 0
    fail_details = []

    try:
        target_issues = search_issues_by_jql(jira_config, jql, "key,status", logger)
        if not target_issues:
            logger.info("未找到符合条件的工单，无需执行状态修改")
            return success_count, fail_count, fail_details

        logger.info(f"\n开始批量处理 {len(target_issues)} 个工单...")
        for issue in target_issues:
            issue_key = issue["key"]
            logger.info(f"\n处理 Issue：{issue_key}")
            if transition_issue_status(jira_config, issue_key, target_status, logger):
                success_count += 1
            else:
                fail_count += 1
                fail_details.append(issue_key)

    except Exception as e:
        logger.error(f"任务执行异常：{str(e)}", exc_info=True)

    return success_count, fail_count, fail_details


def main():
    logger = init_logger()
    target_status = "无需续费"
    total_success = 0
    total_fail = 0
    all_fail_details = []

    try:
        logger.info("=" * 60)
        logger.info(f"Jira工单批量状态流转总程序启动（时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}）")
        logger.info("=" * 60)

        # 1. 加载Jira配置
        jira_config = load_jira_config(logger)

        # 2. 加载父项Issue Key（新增）
        parent_keys = load_parent_issue_keys(logger)

        # 3. 拼接Parent IN条件（新增）
        if parent_keys:
            parent_keys_str = ','.join([f'"{key}"' for key in parent_keys])
            parent_condition = f" AND parent IN ({parent_keys_str})"
        else:
            parent_condition = ""
            logger.warning("未获取到有效父项Key，将不添加parent IN条件")

        # 4. 定义任务（动态拼接JQL）
        tasks = [
            {
                "name": "续费完成 -> 无需续费",
                "jql": f'type = "续费明细" AND status = "续费完成"{parent_condition}'
            },
            {
                "name": "等待分配 -> 无需续费",
                "jql": f'type = "续费明细" AND status = "等待分配"{parent_condition}'
            },
            {
                "name": "一次确认 -> 无需续费",
                "jql": f'type = "续费明细" AND status = "一次确认"{parent_condition}'
            },
            {
                "name": "二次确认 -> 无需续费",
                "jql": f'type = "续费明细" AND status = "二次确认"{parent_condition}'
            },
            {
                "name": "等待续费 -> 无需续费",
                "jql": f'type = "续费明细" AND status = "等待续费"{parent_condition}'
            }
        ]

        # 依次执行每个任务
        for task in tasks:
            s_cnt, f_cnt, f_details = process_issues(
                jira_config,
                task["jql"],
                task["name"],
                target_status,
                logger
            )
            total_success += s_cnt
            total_fail += f_cnt
            all_fail_details.extend(f_details)

            # 单个任务小结
            logger.info("\n" + "=" * 60)
            logger.info(f"任务「{task['name']}」执行小结：")
            logger.info(f"成功：{s_cnt}，失败：{f_cnt}")
            logger.info("=" * 60)

        # 最终汇总
        logger.info("\n" + "=" * 60)
        logger.info(f"所有任务执行完毕 - 最终汇总：")
        logger.info(f"总成功修改状态数：{total_success}")
        logger.info(f"总修改失败数：{total_fail}")
        if all_fail_details:
            logger.info(f"\n失败工单列表：{', '.join(all_fail_details)}")
        logger.info("=" * 60)

    except Exception as e:
        logger.error(f"程序执行异常：{str(e)}", exc_info=True)
    finally:
        logger.info(f"程序结束（时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}）")
        logger.info("=" * 60 + "\n")


if __name__ == "__main__":
    main()