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


def get_jira_renewal_details():
    logger.info("=" * 50)
    logger.info(f"开始执行Jira续费明细工单数据查询（时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}）")

    # 1. 定义文件路径
    current_dir = os.path.dirname(os.path.abspath(__file__))
    # 输出CSV文件名
    output_csv_path = os.path.normpath(os.path.join(current_dir, '../../jira/data/jira_get_need_renewal.csv'))
    # Jira配置文件路径
    jira_config_path = os.path.normpath(os.path.join(current_dir, '../../jira/config/jira_config.ini'))
    # 阿里云配置文件路径
    aliyun_config_path = os.path.normpath(os.path.join(current_dir, '../../jira/config/aliyun_config.ini'))
    # 【新增】父任务CSV文件路径
    parent_issues_csv_path = os.path.normpath(os.path.join(current_dir, '../../jira/data/jira_get_renewal_issues.csv'))

    # 2. 读取Jira配置文件
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
        logger.error(err_msg)
        print(err_msg)
        return

    # 3. 初始化Jira API认证信息
    try:
        jira_domain = config.get('jira', 'domain')
        user_name = config.get('jira', 'user_name')
        access_token = config.get('jira', 'access_token')
        logger.info(f"成功读取Jira配置 - 域名：{jira_domain}，用户名：{user_name}")
    except (configparser.NoSectionError, configparser.NoOptionError) as e:
        err_msg = f"Jira配置文件解析错误：{str(e)}"
        logger.error(err_msg)
        print(err_msg)
        return

    # 4. 读取阿里云配置文件，解析role_session_name与云账号UID的映射关系
    aliyun_uid_mapping = {}  # 格式：{role_session_name: uid}
    try:
        if not os.path.exists(aliyun_config_path):
            err_msg = f"阿里云配置文件不存在 - {aliyun_config_path}"
            logger.error(err_msg)
            print(err_msg)
            return

        aliyun_config = configparser.ConfigParser()
        aliyun_config.read(aliyun_config_path, encoding='utf-8')
        logger.info(f"已加载阿里云配置文件：{aliyun_config_path}")

        # 遍历所有section，解析role_arn提取UID
        for section in aliyun_config.sections():
            if 'role_arn' in aliyun_config[section]:
                role_arn = aliyun_config[section]['role_arn'].strip()
                role_session_name = aliyun_config[section].get('role_session_name', '').strip()
                # 解析role_arn：格式 acs:ram::UID:role/xxx → 提取UID
                if role_arn and role_session_name:
                    arn_parts = role_arn.split('::')
                    if len(arn_parts) >= 2:
                        uid_part = arn_parts[1].split(':')[0]
                        if uid_part.isdigit():  # 确保提取的是数字UID
                            aliyun_uid_mapping[role_session_name] = uid_part
                            logger.info(f"解析阿里云配置：{role_session_name} → {uid_part}")
        logger.info(f"阿里云账号UID映射表：{aliyun_uid_mapping}")
    except Exception as e:
        err_msg = f"读取/解析阿里云配置文件失败：{str(e)}"
        logger.error(err_msg, exc_info=True)
        print(err_msg)
        return

    # 【新增】5. 读取父任务CSV，获取Parent Issue Keys
    parent_issue_keys = []
    try:
        if not os.path.exists(parent_issues_csv_path):
            err_msg = f"父任务CSV文件不存在 - {parent_issues_csv_path}"
            logger.error(err_msg)
            print(err_msg)
            return

        with open(parent_issues_csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            if 'Issue Key' not in reader.fieldnames:
                err_msg = f"父任务CSV中未找到列名：'Issue Key'"
                logger.error(err_msg)
                print(err_msg)
                return

            for row in reader:
                key = row.get('Issue Key', '').strip()
                if key:
                    parent_issue_keys.append(key)

        # 去重
        parent_issue_keys = list(set(parent_issue_keys))

        if not parent_issue_keys:
            err_msg = f"父任务CSV中未读取到有效的'Issue Key'"
            logger.error(err_msg)
            print(err_msg)
            return

        logger.info(f"成功读取到 {len(parent_issue_keys)} 个父任务Key：{parent_issue_keys}")

    except Exception as e:
        err_msg = f"读取父任务CSV文件失败：{str(e)}"
        logger.error(err_msg, exc_info=True)
        print(err_msg)
        return

    # 6. 直接查询所有符合条件的工单
    api_url = f"https://{jira_domain}/rest/api/3/search/jql"
    all_issues = []  # 存储所有工单数据
    total_issues = 0

    try:
        # 【核心修改】构造JQL：拼接 parent IN 子句
        # 将List转换为 ("KEY-1", "KEY-2") 格式的字符串
        parent_keys_str = '", "'.join(parent_issue_keys)
        jql = f'type = "续费明细" AND status = "等待续费" AND parent IN ("{parent_keys_str}")'

        # 保留原字段查询列表
        fields = "key,customfield_10484,customfield_10487,customfield_10918,customfield_10920,customfield_10488,customfield_10813"
        params = {
            "jql": jql,
            "fields": fields,
            "maxResults": 1000  # 单次最大查询1000条，可根据需要调整
        }

        logger.info(f"执行JQL查询：{jql}")
        # 发送API请求
        response = requests.get(
            api_url,
            params=params,
            auth=HTTPBasicAuth(user_name, access_token)
        )
        response.raise_for_status()
        data = response.json()
        issues = data.get('issues', [])

        if not issues:
            logger.info("未查询到符合条件的工单")
        else:
            logger.info(f"查询到{len(issues)}个符合条件的工单")

            # 解析工单数据
            for issue in issues:
                issue_key = issue["key"]
                # 提取自定义字段（处理空值）
                account = issue["fields"].get("customfield_10484") or ""  # 账号
                resource_id = issue["fields"].get("customfield_10487") or ""  # 资源ID
                product_code = issue["fields"].get("customfield_10918") or ""  # 产品代码
                product_type = issue["fields"].get("customfield_10920") or ""  # 产品类型
                region = issue["fields"].get("customfield_10488") or ""  # 地域

                # 续费时长转为整数
                renewal_duration_raw = issue["fields"].get("customfield_10813")
                renewal_duration = ""
                if renewal_duration_raw is not None and renewal_duration_raw != "":
                    try:
                        renewal_duration = int(float(renewal_duration_raw))
                    except (ValueError, TypeError):
                        renewal_duration = ""

                # 匹配云账号UID：根据账号（customfield_10484）匹配role_session_name
                cloud_account_uid = aliyun_uid_mapping.get(account.strip(), "")

                # 构造工单数据字典
                issue_data = {
                    "Issue Key": issue_key,
                    "账号": account,
                    "资源ID": resource_id,
                    "产品代码": product_code,
                    "产品类型": product_type,
                    "地域": region,
                    "续费时长（月）": renewal_duration,
                    "平台": "alibaba",
                    "云账号UID": cloud_account_uid
                }
                all_issues.append(issue_data)
                total_issues += 1

    except requests.exceptions.HTTPError as e:
        err_msg = f"查询工单时HTTP错误：{e}"
        logger.error(f"{err_msg}，响应详情：{response.text if 'response' in locals() else '无'}")
        print(err_msg)
    except Exception as e:
        err_msg = f"查询工单时异常：{str(e)}"
        logger.error(err_msg, exc_info=True)
        print(err_msg)

    # 7. 将工单数据写入CSV文件
    if not all_issues:
        logger.warning("未查询到任何符合条件的工单数据，无需写入CSV")
        print("未查询到任何符合条件的工单数据")
    else:
        try:
            # 确保data目录存在
            os.makedirs(os.path.dirname(output_csv_path), exist_ok=True)
            # 覆盖写入CSV
            with open(output_csv_path, 'w', newline='', encoding='utf-8') as f:
                headers = [
                    'Issue Key', '账号', '资源ID', '产品代码',
                    '产品类型', '地域', '续费时长（月）', '平台', '云账号UID'
                ]
                writer = csv.DictWriter(f, fieldnames=headers)
                writer.writeheader()
                writer.writerows(all_issues)

            logger.info(f"工单数据已写入CSV文件：{output_csv_path}，共{total_issues}条记录")
            print(f"工单数据写入完成，文件路径：{output_csv_path}，共{total_issues}条记录")
        except Exception as e:
            err_msg = f"写入工单CSV文件失败：{str(e)}"
            logger.error(err_msg, exc_info=True)
            print(err_msg)

    # 8. 任务结束
    logger.info("Jira续费明细工单数据查询流程结束")
    logger.info("=" * 50 + "\n")


if __name__ == "__main__":
    get_jira_renewal_details()