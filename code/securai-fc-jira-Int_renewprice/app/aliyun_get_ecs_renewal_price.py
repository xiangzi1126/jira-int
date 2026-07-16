# -*- coding: utf-8 -*-
from alibabacloud_sts20150401.client import Client as StsClient
from alibabacloud_sts20150401.models import AssumeRoleRequest
from alibabacloud_ecs20140526.client import Client as EcsClient
from alibabacloud_ecs20140526 import models as ecs_models
from alibabacloud_tea_openapi import models as open_api_models

import configparser
import os
import sys
import re
import csv
import json
import logging
from typing import List, Dict, Any
from datetime import datetime, timedelta

# ===================== 全局配置与常量定义 =====================
# 数据目录配置
DATA_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/data'))
# 日志文件路径
LOG_FILE = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/log/actual_renewal_price.log'))


# ===================== 可视化日志系统配置 =====================
class VisualLogger:
    """提供极具可视化排版的结构化日志系统"""

    def __init__(self):
        log_dir = os.path.dirname(LOG_FILE)
        os.makedirs(log_dir, exist_ok=True)
        log_format = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

        file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
        file_handler.setFormatter(log_format)

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(log_format)

        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.INFO)
        if not self.logger.handlers:
            self.logger.addHandler(file_handler)
            self.logger.addHandler(console_handler)

    def info(self, msg: str):
        self.logger.info(msg)

    def warning(self, msg: str):
        self.logger.warning(msg)

    def error(self, msg: str):
        self.logger.error(msg)

    def print_block(self, title: str, data: Any, level: int = logging.INFO):
        """深度美化打印 JSON/字典 等结构化数据，用于极致的可视化追踪"""
        border = "═" * 80
        if isinstance(data, (dict, list)):
            try:
                formatted_data = json.dumps(data, indent=4, ensure_ascii=False)
            except Exception:
                formatted_data = str(data)
        else:
            formatted_data = str(data)

        log_msg = f"\n{border}\n❖ {title} ❖\n{border}\n{formatted_data}\n{border}\n"
        self.logger.log(level, log_msg)

    def print_step(self, step_name: str):
        """打印主干流程节点"""
        self.logger.info(f"\n" + "★" * 30 + f" [STEP: {step_name}] " + "★" * 30)


logger = VisualLogger()


# ===================== 核心业务类 =====================
class AliyunEcsRenewalPriceQuery:
    """阿里云ECS续费价格查询工具类 (兼容中国站与国际站)"""

    @staticmethod
    def _get_config_path() -> str:
        return os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/config/aliyun_config.ini'))

    @staticmethod
    def sanitize_session_name(name: str) -> str:
        """清洗 RoleSessionName 以符合阿里云规范"""
        sanitized = re.sub(r'[^a-zA-Z0-9.\-_]', '_', name)
        if len(sanitized) < 2:
            sanitized = f"STS_{sanitized}" if sanitized else "STS_Session"
        return sanitized[:64]

    @staticmethod
    def get_master_credentials() -> tuple[str, str]:
        """从配置文件读取主账号AK/SK"""
        config_path = AliyunEcsRenewalPriceQuery._get_config_path()
        config = configparser.ConfigParser()
        config.read(config_path, encoding='utf-8')

        master_ak_id = config.get('aliyun', 'access_key_id')
        master_ak_secret = config.get('aliyun', 'access_key_secret')
        return master_ak_id, master_ak_secret

    @staticmethod
    def build_account_role_map() -> Dict[str, str]:
        """建立 账号名(role_session_name) 到 role_arn 的映射字典"""
        config_path = AliyunEcsRenewalPriceQuery._get_config_path()
        config = configparser.ConfigParser()
        config.read(config_path, encoding='utf-8')

        account_map = {}
        for section in config.sections():
            if section.startswith('aliyun-'):
                try:
                    role_arn = config.get(section, 'role_arn')
                    role_session_name = config.get(section, 'role_session_name')
                    account_map[role_session_name] = role_arn
                except Exception:
                    pass
        logger.print_block("Account Role Map Loaded", account_map)
        return account_map

    @staticmethod
    def create_ecs_client_by_role(role_arn: str, raw_session_name: str) -> EcsClient:
        """根据指定的 ARN 创建 ECS OpenAPI 客户端 (全站通用Endpoint)"""
        master_ak_id, master_ak_secret = AliyunEcsRenewalPriceQuery.get_master_credentials()
        api_safe_session_name = AliyunEcsRenewalPriceQuery.sanitize_session_name(raw_session_name)

        # 1. 初始化STS客户端 (sts.aliyuncs.com 国际/国内通用)
        sts_config = open_api_models.Config(
            access_key_id=master_ak_id,
            access_key_secret=master_ak_secret,
            endpoint='sts.aliyuncs.com'
        )
        sts_client = StsClient(sts_config)

        # 2. 申请临时凭证
        assume_role_request = AssumeRoleRequest(
            role_arn=role_arn,
            role_session_name=api_safe_session_name,
            duration_seconds=3600
        )
        response = sts_client.assume_role(assume_role_request)

        # 3. 创建ECS客户端 (ecs.aliyuncs.com 国际/国内通用)
        credentials = response.body.credentials
        ecs_config = open_api_models.Config(
            access_key_id=credentials.access_key_id,
            access_key_secret=credentials.access_key_secret,
            security_token=credentials.security_token,
            endpoint='ecs.aliyuncs.com'
        )
        return EcsClient(ecs_config)

    @staticmethod
    def get_time_periods() -> tuple[str, str, str]:
        """获取上个月、当前月、下个月的年月字符串 (YYYY-MM)"""
        today = datetime.today()
        first_day_of_current = today.replace(day=1)

        last_day_of_last = first_day_of_current - timedelta(days=1)
        last_month = last_day_of_last.strftime("%Y-%m")

        this_month = today.strftime("%Y-%m")

        next_month_date = (first_day_of_current + timedelta(days=32)).replace(day=1)
        next_month = next_month_date.strftime("%Y-%m")

        return last_month, this_month, next_month

    @staticmethod
    def load_renewal_params() -> Dict[str, Dict[str, Any]]:
        """从 jira_get_renewal_sbu_issues.csv 读取续费时长参数"""
        jira_csv_path = os.path.join(DATA_DIR, 'jira_get_renewal_sbu_issues.csv')
        renew_params = {}

        if not os.path.exists(jira_csv_path):
            logger.warning(f"Jira 参数文件不存在: {jira_csv_path}，将跳过参数读取")
            return renew_params

        with open(jira_csv_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                instance_id = row.get('ID', '').strip()
                duration_str = row.get('续费时长（月）', '1.0').strip()
                if not instance_id:
                    continue

                try:
                    duration = float(duration_str)
                    if duration == 12.0:
                        renew_params[instance_id] = {'PriceUnit': 'Year', 'Period': 1}
                    elif 1.0 <= duration <= 9.0:
                        renew_params[instance_id] = {'PriceUnit': 'Month', 'Period': int(duration)}
                    else:
                        renew_params[instance_id] = {'PriceUnit': 'Month', 'Period': 1}
                except ValueError:
                    renew_params[instance_id] = {'PriceUnit': 'Month', 'Period': 1}

        logger.info(f"成功加载 {len(renew_params)} 条 Jira 续费参数配置")
        return renew_params

    @staticmethod
    def fetch_ecs_price(client: EcsClient, target: Dict[str, Any]) -> Dict[str, Any]:
        """调用API获取单个ECS资源的续费价格 (国际账号会自动返回 USD)"""
        try:
            resource_type = target.get('产品类型') or 'instance'
            if resource_type.lower() == 'ecs':
                resource_type = 'instance'

            request_params = {
                "region_id": target.get('地域'),
                "resource_id": target.get('资源id'),
                "resource_type": resource_type,
                "period": target.get('Period'),
                "price_unit": target.get('PriceUnit')
            }
            logger.print_block(f"API Request Payload - [{target.get('资源id')}]", request_params)

            request = ecs_models.DescribeRenewalPriceRequest(**request_params)
            response = client.describe_renewal_price(request)
            price_info = response.body.price_info

            original_price = price_info.price.original_price
            discount_price = price_info.price.discount_price
            trade_price = price_info.price.trade_price
            currency = price_info.price.currency  # 关键点：国际站天然返回 USD

            description = ""
            if price_info.rules and price_info.rules.rule:
                rules = price_info.rules.rule
                description = " | ".join([r.description for r in rules if r.description])

            result = {
                '原价': original_price,
                '折扣': discount_price,
                '货币单位': currency,
                '最终价': trade_price,
                '描述': description
            }
            logger.print_block(f"API Response Result - [{target.get('资源id')}]", result)
            return result

        except Exception as e:
            logger.error(f"资源 {target.get('资源id')} 询价失败: {str(e)}")
            return {
                '原价': '', '折扣': '', '货币单位': '', '最终价': '', '描述': f'Error: {str(e)}'
            }

    @staticmethod
    def main():
        logger.print_step("PROGRAM START: ECS RENEWAL PRICE QUERY (GLOBAL EDITION)")

        last_month, this_month, next_month = AliyunEcsRenewalPriceQuery.get_time_periods()
        time_info = {"Last Month": last_month, "This Month": this_month, "Target Expiry Month": next_month}
        logger.print_block("Time Period Calculation", time_info)

        # 1. 加载参数
        renew_params = AliyunEcsRenewalPriceQuery.load_renewal_params()
        account_map = AliyunEcsRenewalPriceQuery.build_account_role_map()

        # 2. 筛选数据
        bill_csv_path = os.path.join(DATA_DIR, f'aliyun_renewal_bill_{last_month}.csv')
        if not os.path.exists(bill_csv_path):
            logger.error(f"找不到账单文件: {bill_csv_path}")
            sys.exit(1)

        target_resources = []
        with open(bill_csv_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get('产品代码', '').lower() != 'ecs':
                    continue
                if not row.get('资源到期时间', '').startswith(next_month):
                    continue

                res_id = row.get('资源id', '').strip()
                if res_id in renew_params:
                    target = {
                        '资源id': res_id,
                        '资源所属账号': row.get('资源所属账号', '').strip(),
                        '资源到期时间': row.get('资源到期时间', ''),
                        '产品代码': row.get('产品代码', ''),
                        '产品类型': row.get('产品类型', ''),
                        '地域': row.get('地域', ''),
                        'PriceUnit': renew_params[res_id]['PriceUnit'],
                        'Period': renew_params[res_id]['Period']
                    }
                    target_resources.append(target)

        logger.info(f"成功筛选出需要询价的 ECS 资源数量: {len(target_resources)}")

        # 3. 按账号分组
        grouped_targets = {}
        for target in target_resources:
            acc = target['资源所属账号']
            grouped_targets.setdefault(acc, []).append(target)

        results = []

        # 4. API 询价
        logger.print_step("STARTING API QUERY PROCESS")
        for account, instances in grouped_targets.items():
            if account not in account_map:
                logger.warning(f"账号 {account} 未映射 Role_ARN，跳过旗下 {len(instances)} 个资源。")
                continue

            role_arn = account_map[account]
            logger.info(f"▶▶ 切换账号凭证: {account} | ARN: {role_arn} | 资源数: {len(instances)}")

            try:
                client = AliyunEcsRenewalPriceQuery.create_ecs_client_by_role(role_arn, account)

                for target in instances:
                    price_result = AliyunEcsRenewalPriceQuery.fetch_ecs_price(client, target)
                    final_row = {
                        '资源id': target['资源id'],
                        '资源所属账号': target['资源所属账号'],
                        '资源到期时间': target['资源到期时间'],
                        '产品代码': target['产品代码'],
                        '描述': price_result['描述'],
                        '原价': price_result['原价'],
                        '折扣': price_result['折扣'],
                        '货币单位': price_result['货币单位'],
                        '最终价': price_result['最终价']
                    }
                    results.append(final_row)

            except Exception as e:
                logger.error(f"处理账号 {account} 发生异常: {str(e)}")

        # 5. 输出 CSV
        logger.print_step("SAVING RESULTS")
        output_csv_path = os.path.join(DATA_DIR, f'aliyun_renewal_price_{this_month}.csv')
        output_fields = ['资源id', '资源所属账号', '资源到期时间', '产品代码', '描述', '原价', '折扣', '货币单位',
                         '最终价']

        file_exists = os.path.exists(output_csv_path)
        with open(output_csv_path, 'a', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=output_fields)
            if not file_exists:
                writer.writeheader()
            writer.writerows(results)

        logger.print_block(f"EXECUTION COMPLETE", {
            "Total Inquiries Success": len(results),
            "Output Path": output_csv_path,
            "Write Mode": "Append ('a')"
        })


if __name__ == '__main__':
    AliyunEcsRenewalPriceQuery.main()