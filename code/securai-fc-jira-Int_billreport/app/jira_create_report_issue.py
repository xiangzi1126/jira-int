import requests
from requests.auth import HTTPBasicAuth
import configparser
import os
import logging
from datetime import datetime
import pandas as pd
import numpy as np
import json
import re  # 新增：用于文件夹名非法字符清理


# 增强：处理 JSON 序列化及通用字段转换（修复pandas类型判断错误）
def clean_json_value(value):
    if pd.isna(value) or value is None:
        return ""
    # 处理pandas扩展数值类型（改用numpy类型判断）
    elif isinstance(value, (pd.Int64Dtype, pd.Float64Dtype)):
        val = value.item() if not pd.isna(value) else ""
        return str(val)  # 转为字符串
    # 处理numpy数值类型
    elif isinstance(value, (np.int64, np.int32, np.int16)):
        return str(int(value))  # 转为字符串
    elif isinstance(value, (np.float64, np.float32)):
        return str(float(value))  # 转为字符串
    # 基础数值类型：统一转为字符串（关键修改）
    elif isinstance(value, (float, int)):
        return str(value)
    # 避免pandas数据结构
    elif isinstance(value, (pd.Series, pd.DataFrame)):
        return str(value)
    # 布尔类型特殊处理（保持布尔值）
    elif isinstance(value, bool):
        return value
    # 其他类型转为字符串
    else:
        return str(value)


# 初始化日志配置
def init_logger():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.normpath(os.path.join(current_dir, '../../jira/log'))
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'create_report_issue.log')
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.FileHandler(log_file, encoding='utf-8'), logging.StreamHandler()]
    )
    return logging.getLogger(__name__)


logger = init_logger()


# 上传附件函数（完全复用原有逻辑，支持单文件上传，批量循环调用即可）
def upload_jira_attachment(jira_domain, issue_key, file_path, api_user, api_token):
    """调用Jira API上传附件到指定issue"""
    try:
        attachment_url = f"https://{jira_domain}/rest/api/3/issue/{issue_key}/attachments"
        headers = {"X-Atlassian-Token": "no-check"}

        if not os.path.exists(file_path):
            logger.error(f"附件文件不存在：{file_path}")
            return False

        with open(file_path, 'rb') as file:
            files = {'file': (os.path.basename(file_path), file)}
            response = requests.post(
                attachment_url,
                headers=headers,
                files=files,
                auth=HTTPBasicAuth(api_user, api_token)
            )
            response.raise_for_status()
            logger.info(f"附件上传成功：{os.path.basename(file_path)} -> {issue_key}")
            return True
    except Exception as e:
        logger.error(f"附件上传失败（{issue_key}）：{str(e)}", exc_info=True)
        return False


def create_monthly_billing_issue():
    logger.info("=" * 50)
    logger.info(f"开始执行月报类Jira工作项创建操作（时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}）")

    # 写死固定配置
    ISSUE_TYPE_NAME = "月报：课金代行"
    REQUEST_TYPE_NAME = "月报：课金代行"
    logger.info(f"使用固定配置 - 问题类型：{ISSUE_TYPE_NAME}，请求类型：{REQUEST_TYPE_NAME}")

    # 读取配置文件
    current_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.normpath(os.path.join(current_dir, '../../jira/config/jira_config.ini'))
    if not os.path.exists(config_path):
        err_msg = f"配置文件不存在 - {config_path}"
        logger.error(err_msg)
        print(err_msg)
        return

    config = configparser.ConfigParser()
    try:
        config.read(config_path, encoding='utf-8')
        jira_domain = config.get('jira', 'domain')
        api_user = config.get('jira', 'user_name')
        api_token = config.get('jira', 'access_token')
        logger.info(f"已加载Jira配置：域名={jira_domain}，用户名={api_user}")
    except Exception as e:
        err_msg = f"读取配置文件失败：{str(e)}"
        logger.error(err_msg)
        print(err_msg)
        return

    try:
        # 计算上个月的年月（用于匹配账单和报表文件）
        today = datetime.now()
        if today.month == 1:
            last_month_year = today.year - 1
            last_month = 12
        else:
            last_month_year = today.year
            last_month = today.month - 1
        target_month = f"{last_month_year}-{last_month:02d}"  # YYYY-MM格式
        target_month_short = f"{last_month_year}{last_month:02d}"  # YYYYMM格式（用于文件名）
        logger.info(f"目标账期：{target_month}（文件名格式：{target_month_short}）")

        # 读取账号映射文件（获取需要创建issue的账号）
        account_file_path = os.path.normpath(os.path.join(current_dir, '../../jira/data/jira_get_account.csv'))
        if not os.path.exists(account_file_path):
            err_msg = f"账号映射文件不存在 - {account_file_path}"
            logger.error(err_msg)
            print(err_msg)
            return
        account_df = pd.read_csv(account_file_path)

        # 读取转售业务详情文件（用于判断是否有月报）
        resale_file_path = os.path.normpath(os.path.join(current_dir, '../../jira/data/resale_business_details.csv'))
        if not os.path.exists(resale_file_path):
            err_msg = f"转售业务详情文件不存在 - {resale_file_path}"
            logger.error(err_msg)
            print(err_msg)
            return
        resale_df = pd.read_csv(resale_file_path)

        # 检查必要列
        required_account_cols = ['资源所属账号', 'Project Key']
        if '标签' not in account_df.columns:
            logger.warning("账号映射文件缺少 '标签' 列，已自动添加空标签列。")
            account_df['标签'] = np.nan  # 确保标签列存在，以便进行组合去重

        if not all(col in account_df.columns for col in required_account_cols):
            err_msg = f"账号映射文件缺少必要列：{required_account_cols}"
            logger.error(err_msg)
            print(err_msg)
            return

        # 检查转售业务详情文件的必要列
        required_resale_cols = ['Project Key', '是否有月报']
        if not all(col in resale_df.columns for col in required_resale_cols):
            err_msg = f"转售业务详情文件缺少必要列：{required_resale_cols}"
            logger.error(err_msg)
            print(err_msg)
            return

        # 清洗转售业务详情的列值（去除空格，统一大小写）
        resale_df['Project Key'] = resale_df['Project Key'].astype(str).str.strip()
        resale_df['是否有月报'] = resale_df['是否有月报'].astype(str).str.strip()
        # 过滤出有月报的Project Key
        resale_has_monthly_report = resale_df[resale_df['是否有月报'] == '有']['Project Key'].unique()
        logger.info(f"转售业务详情中，有月报的Project Key数量：{len(resale_has_monthly_report)}")

        # 过滤账号映射文件：只保留Project Key在有月报列表中的记录
        account_df['Project Key'] = account_df['Project Key'].astype(str).str.strip()
        account_df_filtered = account_df[account_df['Project Key'].isin(resale_has_monthly_report)]
        if account_df_filtered.empty:
            logger.info("没有找到需要创建issue的账号（所有Project Key都没有月报）")
            print("没有找到需要创建issue的账号（所有Project Key都没有月报）")
            return
        logger.info(f"过滤后，需要处理的账号记录数：{len(account_df_filtered)}（原记录数：{len(account_df)}）")

        logger.info(f"成功读取账号映射文件，共 {len(account_df)} 条记录，过滤后剩余 {len(account_df_filtered)} 条")

        # 读取请求类型映射文件（匹配Request TypeID）
        request_type_file_path = os.path.normpath(os.path.join(current_dir, '../../jira/data/jira_request_type.csv'))
        if not os.path.exists(request_type_file_path):
            err_msg = f"请求类型映射文件不存在 - {request_type_file_path}"
            logger.error(err_msg)
            print(err_msg)
            return
        request_type_df = pd.read_csv(request_type_file_path)
        required_request_type_cols = ['项目键', 'Request Type名称', 'Request TypeID']
        if not all(col in request_type_df.columns for col in required_request_type_cols):
            err_msg = f"请求类型文件缺少必要列：{required_request_type_cols}"
            logger.error(err_msg)
            print(err_msg)
            return
        logger.info(f"成功读取请求类型映射文件，共 {len(request_type_df)} 条记录")

        # ************ 关键修改区域：根据“账号+标签”去重 ************
        # 去重列表（一个唯一的账号+标签组合创建一个issue）
        unique_combinations_df = account_df_filtered[['资源所属账号', 'Project Key', '标签']].drop_duplicates()
        logger.info(f"去重后需处理的唯一组合数（账号+标签）：{len(unique_combinations_df)}")

        total_success = 0
        total_fail = 0
        all_fail_records = []

        # 遍历每个唯一的账号+标签组合创建issue
        for index, row in unique_combinations_df.iterrows():
            account = row['资源所属账号']
            project_key = row['Project Key']
            raw_tag = row['标签']
            tag = raw_tag if pd.notna(raw_tag) else ""

            logger.info(f"\n{'=' * 30} 开始处理组合：{account} (标签：{tag if tag else '无标签'}) {'=' * 30}")

            logger.info(f"账号 {account} 匹配到Project Key：{project_key}")

            # 匹配Request TypeID
            match_request_type = request_type_df[
                (request_type_df['项目键'] == project_key) &
                (request_type_df['Request Type名称'] == REQUEST_TYPE_NAME)
                ]
            if match_request_type.empty:
                err_msg = f"项目 {project_key} 未找到请求类型 '{REQUEST_TYPE_NAME}' 对应的ID"
                logger.warning(err_msg)
                total_fail += 1
                all_fail_records.append(f"组合 {account}|{tag} - {err_msg}")
                continue
            request_type_id = match_request_type['Request TypeID'].values[0]
            logger.info(f"匹配到Request TypeID：{request_type_id}")

            # ************ 摘要标签处理逻辑保留，仅删除原单文件名生成逻辑 ************
            # 构造包含标签的 Issue 摘要（保证Jira摘要唯一性）
            tag_suffix_for_summary = ""
            if tag:
                # 摘要使用激进清理：:和=和空格都替换为-
                sanitized_tag_summary = tag.replace(':', '-').replace(' ', '-').replace('=', '-')
                tag_suffix_for_summary = f"_{sanitized_tag_summary}"

            summary = f"课金月报_{account}_{target_month_short}{tag_suffix_for_summary}"
            logger.info(f"创建issue摘要：{summary}")

            # 构造Jira请求数据
            create_url = f"https://{jira_domain}/rest/api/3/issue"
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json"
            }
            issue_data = {
                "fields": {
                    "project": {"key": project_key},
                    "summary": summary,
                    "description": {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": f"自动创建的{target_month}课金代行月报. 标签信息: {tag if tag else '无'}"
                                    }
                                ]
                            }
                        ]
                    },
                    "issuetype": {"name": ISSUE_TYPE_NAME},
                    "customfield_10010": clean_json_value(request_type_id)  # Request TypeID字段
                }
            }
            # 输出issue_data完整内容（控制台+日志）
            logger.info(
                f"组合 {account}|{tag} 待创建的issue_data：\n{json.dumps(issue_data, ensure_ascii=False, indent=2)}")
            print(f"\n组合 {account}|{tag} 待创建的issue_data：")
            print(json.dumps(issue_data, ensure_ascii=False, indent=2))

            # 发送创建issue请求
            try:
                response = requests.post(
                    create_url,
                    json=issue_data,
                    headers=headers,
                    auth=HTTPBasicAuth(api_user, api_token)
                )
                response.raise_for_status()
                issue_key = response.json()["key"]
                logger.info(f"组合 {account}|{tag} 的issue创建成功，工单号：{issue_key}")
                print(f"\n组合 {account}|{tag} - 创建成功，工单号：{issue_key}")

                # ================ 核心修改：批量文件夹附件上传逻辑 ================
                # 1. 生成目标文件夹名（核心规则：有标签=账号-标签，无标签=账号）
                if tag:
                    # 清理Windows/Linux文件夹名非法字符 \ / : * ? " < > |，避免路径报错
                    sanitized_tag_folder = re.sub(r'[\\/:*?"<>|]', '_', tag).strip()
                    # 防止标签清理后为空，导致文件夹名出现「账号-」的异常格式
                    folder_name = f"{account}-{sanitized_tag_folder}" if sanitized_tag_folder else account
                else:
                    folder_name = account

                # 2. 拼接文件夹完整路径
                folder_path = os.path.normpath(os.path.join(current_dir, '../../jira/monthly_report', folder_name))
                logger.info(f"匹配附件文件夹路径：{folder_path}")
                print(f"匹配附件文件夹：{folder_path}")

                # 3. 检查文件夹是否存在
                if not os.path.isdir(folder_path):
                    logger.warning(f"附件文件夹不存在，跳过上传：{folder_path}")
                    print(f"⚠️  附件文件夹不存在，无文件可上传")
                    total_success += 1
                    continue

                # 4. 遍历文件夹内所有文件（过滤子目录+隐藏文件）
                file_list = []
                for item in os.listdir(folder_path):
                    # 跳过系统隐藏文件（.DS_Store、Thumbs.db等）
                    if item.startswith('.'):
                        continue
                    item_full_path = os.path.join(folder_path, item)
                    # 只保留文件，跳过子文件夹
                    if os.path.isfile(item_full_path):
                        file_list.append(item_full_path)

                # 5. 检查是否有可上传的文件
                if not file_list:
                    logger.warning(f"附件文件夹 {folder_path} 内无有效文件，跳过上传")
                    print(f"⚠️  附件文件夹内无有效文件，跳过上传")
                    total_success += 1
                    continue

                # 6. 批量循环上传所有文件
                logger.info(f"找到 {len(file_list)} 个待上传文件，开始批量上传")
                print(f"找到 {len(file_list)} 个待上传文件，开始上传：")
                upload_success = 0
                upload_fail = 0

                for file_path in file_list:
                    file_name = os.path.basename(file_path)
                    # 调用原有上传函数，单文件逐个上传
                    if upload_jira_attachment(jira_domain, issue_key, file_path, api_user, api_token):
                        upload_success += 1
                        print(f"  ✅ {file_name} 上传成功")
                    else:
                        upload_fail += 1
                        print(f"  ❌ {file_name} 上传失败")

                # 7. 输出本次批量上传结果
                logger.info(f"组合 {account}|{tag} 附件批量上传完成：成功{upload_success}个，失败{upload_fail}个")
                print(f"\n📦 附件上传完成：成功{upload_success}个，失败{upload_fail}个")
                # ================ 核心修改结束 ================

                total_success += 1

            except requests.exceptions.HTTPError as e:
                resp_text = response.text if 'response' in locals() else '无'
                err_msg = f"HTTP错误：{e}，响应：{resp_text[:100]}..."
                logger.error(f"组合 {account}|{tag} 创建失败：{err_msg}")
                print(f"\n组合 {account}|{tag} - 创建失败：{err_msg}")
                total_fail += 1
                all_fail_records.append(f"组合 {account}|{tag} - {err_msg}")
            except Exception as e:
                err_msg = f"处理异常：{str(e)}"
                logger.error(f"组合 {account}|{tag} 创建失败：{err_msg}", exc_info=True)
                print(f"\n组合 {account}|{tag} - 创建失败：{err_msg}")
                total_fail += 1
                all_fail_records.append(f"组合 {account}|{tag} - {err_msg}")

        # 输出总体处理结果
        logger.info("\n" + "=" * 50)
        logger.info(f"所有账号处理汇总：")
        logger.info(f"总账号+标签组合数：{len(unique_combinations_df)}")
        logger.info(f"成功创建数：{total_success}")
        logger.info(f"创建失败数：{total_fail}")
        if all_fail_records:
            logger.info("失败详情：")
            for idx, detail in enumerate(all_fail_records, 1):
                logger.info(f"{idx}. {detail}")

        print("\n" + "=" * 50)
        print(f"所有账号处理汇总：")
        print(f"总账号+标签组合数：{len(unique_combinations_df)}")
        print(f"成功创建数：{total_success}")
        print(f"创建失败数：{total_fail}")
        if all_fail_records:
            print("失败详情：")
            for idx, detail in enumerate(all_fail_records, 1):
                print(f"{idx}. {detail}")

    except Exception as e:
        err_msg = f"全局处理异常：{str(e)}"
        logger.error(err_msg, exc_info=True)
        print(err_msg)
    finally:
        logger.info("月报类Jira工作项创建操作流程结束")
        logger.info("=" * 50 + "\n")


if __name__ == "__main__":
    create_monthly_billing_issue()