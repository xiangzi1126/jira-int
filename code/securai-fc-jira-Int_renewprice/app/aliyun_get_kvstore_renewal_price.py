# -*- coding: utf-8 -*-
"""
阿里云 KVStore (Redis/Tair) 真实续费开销询价工具 (原生 DescribePrice 国际/国内通用版)
功能：
1. 完全对齐 VPN/EIP 脚本的 STS 认证逻辑（通过主账号 AK 换取子账号角色令牌）。
2. 调用 R-KVStore 原生接口 DescribeInstances 实时拉取实例物理配置 (规格、分片数)。
3. 【彻底重构】：摒弃复杂的 BSS ModuleList 组装，全面拥抱原生 DescribePrice API。
4. 【精准降维】：针对集群版自动下发 ShardCount (分片数)，无缝获取精准续费开销。
5. 【优惠明细追加】：在 Description 中完整提取并追加合同折扣、活动名及优惠券信息。
6. 【极具可视化的探针全显】：强制以高可读性格式在控制台全量输出属性字典与询价 Payload。
7. 【时间筛选增强】：自动计算并严格过滤出“下月到期”的资源进行精准询价。
"""

import os
import sys
import csv
import re
import json
import configparser
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any, Tuple

# ===================== 阿里云 SDK 依赖导入 =====================
from alibabacloud_sts20150401.client import Client as StsClient
from alibabacloud_sts20150401.models import AssumeRoleRequest
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models
from alibabacloud_r_kvstore20150101.client import Client as KVStoreClient
from alibabacloud_r_kvstore20150101 import models as kvstore_models

# ===================== 全局配置与路径定义 =====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.normpath(os.path.join(BASE_DIR, '../../jira/config/aliyun_config.ini'))
DATA_DIR = os.path.normpath(os.path.join(BASE_DIR, '../../jira/data'))
LOG_FILE = os.path.normpath(os.path.join(BASE_DIR, '../../jira/log/kvstore_renewal_price.log'))


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

        self.logger = logging.getLogger('kvstore_renewal_visual')
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
class AliyunKVStoreRenewalPriceQuery:
    # ================== 完美对齐的认证逻辑 ==================
    @staticmethod
    def sanitize_session_name(name: str) -> str:
        sanitized = re.sub(r'[^a-zA-Z0-9.\-_]', '_', name)
        return sanitized[:64] if len(sanitized) >= 2 else "STS_Session"

    @staticmethod
    def get_master_credentials() -> Tuple[str, str]:
        logger.info(f"⚙️ 正在从主配置路径读取AK凭证: {CONFIG_PATH}")
        config = configparser.ConfigParser()
        config.read(CONFIG_PATH, encoding='utf-8')
        try:
            return config.get('aliyun', 'access_key_id'), config.get('aliyun', 'access_key_secret')
        except Exception as e:
            logger.error(f"❌ 主账号配置读取失败: {str(e)}")
            raise

    @staticmethod
    def get_sts_credentials_by_role(role_section: str) -> Any:
        logger.info(f"🔄 正在为配置节点 [{role_section}] 换取 STS 临时令牌...")
        master_ak_id, master_ak_secret = AliyunKVStoreRenewalPriceQuery.get_master_credentials()
        config = configparser.ConfigParser()
        config.read(CONFIG_PATH, encoding='utf-8')
        role_arn = config.get(role_section, 'role_arn')
        raw_session_name = config.get(role_section, 'role_session_name')

        api_safe_session_name = AliyunKVStoreRenewalPriceQuery.sanitize_session_name(raw_session_name)
        sts_config = open_api_models.Config(access_key_id=master_ak_id, access_key_secret=master_ak_secret,
                                            endpoint='sts.aliyuncs.com')
        sts_client = StsClient(sts_config)

        assume_role_request = AssumeRoleRequest(role_arn=role_arn, role_session_name=api_safe_session_name,
                                                duration_seconds=3600)
        return sts_client.assume_role(assume_role_request).body.credentials

    @staticmethod
    def get_session_name_to_section_map() -> Dict[str, str]:
        config = configparser.ConfigParser()
        config.read(CONFIG_PATH, encoding='utf-8')
        mapping = {}
        for section in config.sections():
            if section.startswith('aliyun-') and config.has_option(section, 'role_session_name'):
                session_name = config.get(section, 'role_session_name').strip()
                if session_name:
                    mapping[session_name] = section
        logger.print_block("Loaded Account Role Map", mapping)
        return mapping

    @staticmethod
    def load_duration_mapping() -> Dict[str, float]:
        sbu_csv_path = os.path.join(DATA_DIR, 'jira_get_renewal_sbu_issues.csv')
        duration_map = {}
        if not os.path.exists(sbu_csv_path):
            logger.warning(f"Jira 参数文件不存在: {sbu_csv_path}，将跳过参数读取")
            return duration_map
        try:
            with open(sbu_csv_path, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    instance_id = row.get('ID', '').strip()
                    duration_str = row.get('续费时长（月）', '1.0').strip()
                    if instance_id:
                        try:
                            duration_map[instance_id] = float(duration_str)
                        except ValueError:
                            duration_map[instance_id] = 1.0
        except Exception as e:
            logger.error(f"❌ 读取工单失败: {str(e)}")

        logger.info(f"成功加载 {len(duration_map)} 条 Jira 续费参数配置")
        return duration_map

    # ================== KVStore 专属底层探针 ==================
    @staticmethod
    def fetch_kvstore_physical_specs(kv_client: KVStoreClient, region_id: str, instance_id: str) -> dict:
        """调用 KVStore 接口精准提取实例分片数 (ShardCount) 和规格"""
        logger.info(f"🔍 [KVStore Probe] 正在向地域 [{region_id}] 检索明细: {instance_id}")

        request = kvstore_models.DescribeInstancesRequest(
            region_id=region_id,
            instance_ids=instance_id
        )
        response = kv_client.describe_instances_with_options(request, util_models.RuntimeOptions())
        res_body = response.body.to_map() if hasattr(response.body, 'to_map') else dict(response.body)

        instances = res_body.get('Instances', {}).get('KVStoreInstance', [])
        if not instances:
            raise ValueError(f"API 探测返回为空，未能找到实例 {instance_id} 的信息")

        inst = instances[0]

        # 完美契合规范：极具可视化排版的全量探针数据输出
        logger.print_block(f"DescribeInstances 全量探针数据 - [{instance_id}]", inst)

        specs = {
            'Region': region_id
        }

        # 精准提取 ShardCount 分片数
        shard_count = int(inst.get('ShardCount', 1))
        specs['ShardCount'] = shard_count if shard_count > 0 else 1

        # 优先读取 ShardClass，若无则使用 InstanceClass
        real_shard_class = inst.get('ShardClass', '')
        instance_class = inst.get('InstanceClass', '')

        if real_shard_class:
            specs['ShardClass'] = real_shard_class
        elif instance_class:
            specs['ShardClass'] = instance_class

        # 存储版识别逻辑
        capacity = int(inst.get('Capacity', 0))
        if capacity > 0 and ('essd' in str(instance_class).lower() or 'scm' in str(instance_class).lower()):
            gb_capacity = int(capacity / 1024) if capacity >= 1024 else 1
            specs['Storage'] = str(gb_capacity)

        logger.info(f"👉 [KVStore Probe] 提取出的规格指纹: {specs}")
        return specs

    # ================== 核心原生 API 询价逻辑 ==================
    @staticmethod
    def query_kvstore_price_via_describe_price(
            kv_client: KVStoreClient,
            item: Dict[str, str],
            period_months: int
    ) -> Tuple[float, float, str, str]:

        instance_id = item.get('资源id', '').strip()
        region_id = item.get('地域', item.get('资源所在地域', 'cn-hangzhou')).strip()

        try:
            # 1. 探测指纹 (获取 ShardCount 和规格描述)
            specs = AliyunKVStoreRenewalPriceQuery.fetch_kvstore_physical_specs(kv_client, region_id, instance_id)
            shard_count = specs.get('ShardCount', 1)

            # 组装用于 CSV 的直观描述
            if 'Storage' in specs:
                spec_desc = f"存储规格: {specs.get('Storage')}GB | 分片数: {shard_count}"
            else:
                spec_desc = f"规格: {specs.get('ShardClass')} | 分片数: {shard_count}"

            # 2. 组装原生 DescribePriceRequest
            request_params = {
                "region_id": region_id,
                "order_type": 'RENEW',
                "period": period_months,
                "charge_type": 'PrePaid',
                "shard_count": shard_count,
                "instance_id": instance_id
            }
            logger.print_block(f"原生询价 Payload 提交至 DescribePrice API - [{instance_id}]", request_params)

            request = kvstore_models.DescribePriceRequest(**request_params)

            # 3. 发送请求
            response = kv_client.describe_price_with_options(request, util_models.RuntimeOptions())
            res_body = response.body.to_map() if hasattr(response.body, 'to_map') else dict(response.body)

            # 4. 解析结果 (原生 API 将金额放在 Order 节点中)
            order = res_body.get('Order', {})
            logger.print_block(f"Raw Order Response - [{instance_id}]", order)

            if 'TradeAmount' in order:
                trade_price = float(order.get('TradeAmount', 0.0))
                original_price = float(order.get('OriginalAmount', 0.0))
                currency = order.get('Currency', 'CNY')  # 国际站自动返回 USD

                # 提取并追加优惠详情
                promotions = []
                rules_obj = res_body.get('Rules', {})
                rules_list = rules_obj.get('Rule', []) if isinstance(rules_obj, dict) else []
                for r in rules_list:
                    rule_name = r.get('Title') or r.get('Name')
                    if rule_name and rule_name not in promotions:
                        promotions.append(str(rule_name))

                coupons_obj = order.get('Coupons', {})
                coupons_list = coupons_obj.get('Coupon', []) if isinstance(coupons_obj, dict) else []
                for c in coupons_list:
                    coupon_name = c.get('Description') or c.get('Name') or c.get('CouponNo')
                    if coupon_name and coupon_name not in promotions:
                        promotions.append(f"抵扣券:{coupon_name}")

                if promotions:
                    spec_desc += f" | 🎁命中优惠: {'，'.join(promotions)}"
                elif original_price > trade_price:
                    spec_desc += f" | 🎁命中系统默认折扣"

                return trade_price, original_price, currency, spec_desc
            else:
                msg = res_body.get('Message', 'API 询价失败 (未返回价格)')
                code = res_body.get('Code', '')
                logger.error(f"❌ [API 返回异常] 状态码: {code} | 详情: {msg}")
                return -1.0, -1.0, f"原生API拒绝: [{code}] {msg}", spec_desc

        except Exception as e:
            logger.error(f"❌ [询价异常] {str(e)}")
            return -1.0, -1.0, f"原生询价崩溃: {str(e)}", ""

    # ================== 主控流程 ==================
    @staticmethod
    def main():
        logger.print_step("PROGRAM START: KVSTORE (REDIS/TAIR) RENEWAL PRICE QUERY (GLOBAL EDITION)")

        today = datetime.today()
        first_day_of_current = today.replace(day=1)
        last_month_str = (first_day_of_current - timedelta(days=1)).strftime("%Y-%m")
        input_csv_path = os.path.join(DATA_DIR, f'aliyun_renewal_bill_{last_month_str}.csv')

        first_day_of_next_month = (first_day_of_current + timedelta(days=32)).replace(day=1)
        next_month_str = first_day_of_next_month.strftime("%Y-%m")
        current_month_str = today.strftime("%Y-%m")
        output_csv_path = os.path.join(DATA_DIR, f'aliyun_renewal_price_{current_month_str}.csv')

        time_info = {
            "Last Month (Source Bill)": last_month_str,
            "Current Month": current_month_str,
            "Target Expiry Month (Filter)": next_month_str
        }
        logger.print_block("Time Period Calculation", time_info)

        if not os.path.exists(input_csv_path):
            logger.error(f"❌ 致命阻断: 未找到前置到期数据清单 {input_csv_path}。")
            sys.exit(1)

        session_to_section = AliyunKVStoreRenewalPriceQuery.get_session_name_to_section_map()
        duration_map = AliyunKVStoreRenewalPriceQuery.load_duration_mapping()

        instances_by_account = {}
        filtered_count = 0
        total_count = 0

        with open(input_csv_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                total_count += 1
                instance_id = row.get('资源id', '').strip()
                product_code = row.get('产品代码', '').lower().strip()
                account = row.get('资源所属账号', '').strip()
                expire_time_raw = row.get('资源到期时间', '').strip()

                expire_time_norm = expire_time_raw.replace('/', '-')

                if product_code in ['kvstore', 'redis', 'tair'] or instance_id.startswith('r-'):
                    if account:
                        if expire_time_norm.startswith(next_month_str):
                            if account not in instances_by_account:
                                instances_by_account[account] = []
                            instances_by_account[account].append(row)
                            filtered_count += 1

        logger.info(
            f"📊 基础过滤完成：CSV 共扫描 {total_count} 行，筛选出 {filtered_count} 个符合【KVStore/Redis + 下月到期】条件的实例。")

        output_fields = [
            '资源id', '资源所属账号', '资源到期时间', '产品代码',
            '描述', '原价', '折扣', '货币单位', '最终价'
        ]

        file_exists = os.path.exists(output_csv_path)
        with open(output_csv_path, 'a', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=output_fields)
            if not file_exists:
                writer.writeheader()

        logger.print_step("STARTING API QUERY PROCESS")
        for account, items in instances_by_account.items():
            logger.info(f"\n▶▶ 🏁 账号集群: [{account}] | 待处理KVStore实例总数: {len(items)}")
            section = session_to_section.get(account)
            if not section:
                continue

            try:
                credentials = AliyunKVStoreRenewalPriceQuery.get_sts_credentials_by_role(section)
                open_config = open_api_models.Config(
                    access_key_id=credentials.access_key_id,
                    access_key_secret=credentials.access_key_secret,
                    security_token=credentials.security_token
                )
            except Exception as e:
                logger.error(f"❌ 初始化失败: {str(e)}")
                continue

            processed_rows = []
            for item in items:
                instance_id = item.get('资源id', '').strip()
                region_id = item.get('地域', item.get('资源所在地域', 'cn-hangzhou')).strip()

                try:
                    open_config.endpoint = f'r-kvstore.{region_id}.aliyuncs.com'
                    kv_client = KVStoreClient(open_config)
                except Exception as e:
                    logger.error(f"❌ KVStore Client 区域 {region_id} 初始化失败: {str(e)}")
                    continue

                duration_val = duration_map.get(instance_id, 1.0)
                period_months = int(duration_val) if duration_val > 0 else 1

                trade_price, original_price, status_or_currency, spec_desc = AliyunKVStoreRenewalPriceQuery.query_kvstore_price_via_describe_price(
                    kv_client, item, period_months
                )

                out_row = {
                    '资源id': instance_id,
                    '资源所属账号': account,
                    '资源到期时间': item.get('资源到期时间', ''),
                    '产品代码': item.get('产品代码', 'kvstore'),
                    '描述': '', '原价': '', '折扣': '', '货币单位': '', '最终价': ''
                }

                if trade_price >= 0:
                    out_row['最终价'] = trade_price
                    out_row['原价'] = original_price
                    out_row['货币单位'] = status_or_currency

                    discount = round(original_price - trade_price, 2)
                    out_row['折扣'] = discount if discount > 0 else 0.0
                    out_row['描述'] = spec_desc
                    logger.info(f"✅ 询价成功! ID: {instance_id} | 最终价: {trade_price} {status_or_currency}")
                else:
                    out_row['描述'] = f"API受阻: {status_or_currency}"
                    logger.error(f"❌ API拒绝: ID: {instance_id} | {status_or_currency}")

                processed_rows.append(out_row)

            with open(output_csv_path, 'a', encoding='utf-8-sig', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=output_fields)
                writer.writerows(processed_rows)

        logger.print_step("EXECUTION COMPLETE")
        logger.print_block("Final Details", {
            "Total Inquiries Success": filtered_count,
            "Output Path": output_csv_path,
            "Write Mode": "Append ('a')"
        })


if __name__ == '__main__':
    AliyunKVStoreRenewalPriceQuery.main()