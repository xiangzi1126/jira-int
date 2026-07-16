# -*- coding: utf-8 -*-
"""
阿里云包月 路由器接口 (Router Interface / ri) 开放 API 真实续费开销询价工具
功能：
1. 完美对接 STS 认证逻辑（主账号 AK 换取子账号临时令牌）。
2. 调用 VPC 接口 DescribeRouterInterfaces 实时拉取 RI 规格 (Bandwidth, Role, RouterType, OppositeRegionId)。
3. 【BSS 计费槽位精准翻译】：将标准 RegionId 精准翻译为 BSS 字典所要求的计费代码 (如 ap-northeast-jp59-a01)。
4. 【终极修正版】：严格按照探针 JSON 组装 ModuleList，规避无效字段。
5. 【时间筛选增强】：自动计算并严格过滤出“下月到期”的资源进行精准询价。
"""
import os
import sys
import csv
import re
import configparser
import logging
import json
from datetime import datetime, timedelta
from typing import List, Dict, Any, Tuple

# ===================== 阿里云 SDK 依赖导入 =====================
from alibabacloud_sts20150401.client import Client as StsClient
from alibabacloud_sts20150401.models import AssumeRoleRequest
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models

from alibabacloud_bssopenapi20171214.client import Client as BssOpenApi20171214Client
from alibabacloud_bssopenapi20171214 import models as bss_open_api_20171214_models

from alibabacloud_vpc20160428.client import Client as Vpc20160428Client
from alibabacloud_vpc20160428 import models as vpc_20160428_models

# ===================== 全局配置与路径定义 =====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.normpath(os.path.join(BASE_DIR, '../../jira/config/aliyun_config.ini'))
DATA_DIR = os.path.normpath(os.path.join(BASE_DIR, '../../jira/data'))
LOG_FILE = os.path.normpath(os.path.join(BASE_DIR, '../../jira/log/ri_renewal_price.log'))

# ===================== 基于官方 BSS API 的严谨映射字典 =====================
# 依据 DescribePricingModuleRequest 真实返回数据构建，绝不盲猜
BSS_REGION_MAP = {
    "cn-qingdao": "cn-qingdao-cm5-a01",
    "cn-beijing": "cn-beijing-btc-a01",
    "cn-zhangjiakou": "cn-zhangjiakou-na62-a01",
    "cn-huhehaote": "cn-huhehaote-nt12-a01",
    "cn-hangzhou": "cn-hangzhou-dg-a01",
    "cn-shanghai": "cn-shanghai-eu13-a01",
    "cn-shenzhen": "cn-shenzhen-st3-a01",
    "cn-hongkong": "cn-hongkong-am4-c04",
    "ap-southeast-1": "ap-southeast-os30-a01",  # 新加坡
    "ap-southeast-3": "ap-southeast-my88-a01",  # 吉隆坡
    "ap-southeast-5": "ap-southeast-id35-a01",  # 雅加达
    "ap-northeast-1": "ap-northeast-jp59-a01",  # 东京
    "us-west-1": "us-west-ot7-a01",  # 硅谷
    "us-east-1": "us-east-us44-a01",  # 弗吉尼亚
    "eu-central-1": "eu-central-de46-a01",  # 法兰克福
    "eu-west-1": "eu-west-1-gb33-a01",  # 伦敦
    "me-east-1": "me-east-db47-a01",  # 迪拜
    "ap-hochiminh": "ap-hochiminh-ant",  # 胡志明
    "ap-southeast-6": "ap-southeast-6",  # 马尼拉
    "ap-southeast-7": "ap-southeast-7",  # 曼谷
    "ap-northeast-2": "ap-northeast-2",  # 首尔
    "cn-xian": "cn-xian",  # 西安
    "rus-west-1": "rus-west-1-ru151-a01"  # 莫斯科
}


def init_logger() -> logging.Logger:
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    log_format = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
    file_handler.setFormatter(log_format)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_format)

    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


logger = init_logger()


class VisualLogger:
    """极具可视化排版的详细日志系统"""

    @staticmethod
    def print_panel(title: str, data: Any, level: str = 'info'):
        border = "═" * 80
        msg = f"\n{border}\n ❖ {title} ❖\n{border}\n"
        if isinstance(data, (dict, list)):
            msg += json.dumps(data, ensure_ascii=False, indent=4)
        else:
            msg += str(data)
        msg += f"\n{border}"
        if level == 'info':
            logger.info(msg)
        elif level == 'error':
            logger.error(msg)
        else:
            logger.debug(msg)


class AliyunRiRenewalPriceQuery:
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
        master_ak_id, master_ak_secret = AliyunRiRenewalPriceQuery.get_master_credentials()
        config = configparser.ConfigParser()
        config.read(CONFIG_PATH, encoding='utf-8')
        role_arn = config.get(role_section, 'role_arn')
        raw_session_name = config.get(role_section, 'role_session_name')

        api_safe_session_name = AliyunRiRenewalPriceQuery.sanitize_session_name(raw_session_name)
        sts_config = open_api_models.Config(
            access_key_id=master_ak_id,
            access_key_secret=master_ak_secret,
            endpoint='sts.aliyuncs.com'
        )
        sts_client = StsClient(sts_config)

        assume_role_request = AssumeRoleRequest(
            role_arn=role_arn,
            role_session_name=api_safe_session_name,
            duration_seconds=3600
        )
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
        return mapping

    @staticmethod
    def load_duration_mapping() -> Dict[str, float]:
        sbu_csv_path = os.path.join(DATA_DIR, 'jira_get_renewal_sbu_issues.csv')
        duration_map = {}
        if not os.path.exists(sbu_csv_path):
            logger.warning(f"⚠️ 警告: 未找到 Jira 周期工单文件 {sbu_csv_path}，所有资源将默认按 1.0 个月进行询价。")
            return duration_map

        try:
            with open(sbu_csv_path, 'r', encoding='utf-8') as f:
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
        return duration_map

    @staticmethod
    def fetch_ri_physical_specs(vpc_client: Vpc20160428Client, region_id: str, instance_id: str) -> Dict[str, Any]:
        """调用 VPC 接口精准提取 Router Interface (ri) 的所有计费强关联属性"""
        logger.info(f"🔍 [VPC Probe] 正在向地域 [{region_id}] 检索路由器接口规格明细: {instance_id}")
        try:
            request = vpc_20160428_models.DescribeRouterInterfacesRequest(
                region_id=region_id,
                filter=[
                    vpc_20160428_models.DescribeRouterInterfacesRequestFilter(
                        key="RouterInterfaceId",
                        value=[instance_id]
                    )
                ]
            )
            response = vpc_client.describe_router_interfaces_with_options(request, util_models.RuntimeOptions())
            res_body = response.body.to_map()

            ri_list = res_body.get('RouterInterfaceSet', {}).get('RouterInterfaceType', [])
            if not ri_list:
                raise ValueError("VPC 未能返回此路由器接口记录，资源可能已释放或无权限访问。")

            ri_info = ri_list[0]
            VisualLogger.print_panel(f"VPC DescribeRouterInterfaces Result - [{instance_id}]", ri_info)

            # 核心参数提取
            bandwidth = str(ri_info.get('Bandwidth', ''))
            router_type = ri_info.get('RouterType', 'VRouter')
            opposite_region_id = ri_info.get('OppositeRegionId', '')
            role = ri_info.get('Role', 'InitiatingSide')

            # 依据 BSS 字典执行极其严格的翻译，绝不盲猜
            bss_region = BSS_REGION_MAP.get(region_id)
            bss_opposite_region = BSS_REGION_MAP.get(opposite_region_id)

            if not bss_region:
                raise ValueError(f"当前 RegionId '{region_id}' 无法在 BSS 字典中找到对应的计费槽位代码！")
            if not bss_opposite_region:
                raise ValueError(
                    f"对端 OppositeRegionId '{opposite_region_id}' 无法在 BSS 字典中找到对应的计费槽位代码！")

            specs = {
                'Bandwidth': bandwidth,
                'Role': 'InitiatingSide',  # 续费动作恒定为发起端/计费端
                'Router_Type': router_type,
                'Region': bss_region,
                'Opposite_Region': bss_opposite_region
            }
            logger.info(f"👉 [VPC Probe] 探测成功 -> 提取出的 RI 计费规格指纹 (已翻译): {specs}")
            return specs
        except Exception as e:
            logger.error(f"❌ [VPC Probe] 探测崩溃: {str(e)}")
            raise

    @staticmethod
    def query_ri_price_via_bss(
            bss_client: BssOpenApi20171214Client,
            vpc_client: Vpc20160428Client,
            item: Dict[str, str],
            period_unit: str,
            period_quantity: int
    ) -> Tuple[float, float, str, Dict[str, Any]]:
        """组合多组件进行 RI 询价"""
        instance_id = item.get('资源id', '').strip()
        region_id = item.get('地域', '').strip()

        try:
            # 1. 获取物理核心指纹并转换为 BSS 所需格式
            specs = AliyunRiRenewalPriceQuery.fetch_ri_physical_specs(vpc_client, region_id, instance_id)

            # 2. 严格按 BSS OpenAPI JSON 要求构建单一 Bandwidth 模块与内嵌 Config
            # 格式必需为: Role:InitiatingSide,Opposite_Region:xx,Bandwidth:xx,Router_Type:xx,Region:xx
            config_str = (
                f"Role:{specs['Role']},"
                f"Opposite_Region:{specs['Opposite_Region']},"
                f"Bandwidth:{specs['Bandwidth']},"
                f"Router_Type:{specs['Router_Type']},"
                f"Region:{specs['Region']}"
            )

            module_bandwidth = bss_open_api_20171214_models.GetSubscriptionPriceRequestModuleList(
                module_code='Bandwidth',
                config=config_str
            )

            # 3. 发送询价请求
            request = bss_open_api_20171214_models.GetSubscriptionPriceRequest(
                subscription_type='Subscription',
                service_period_unit=period_unit,
                product_code='ri',
                order_type='Renewal',
                instance_id=instance_id,
                module_list=[module_bandwidth],
                region=region_id,
                service_period_quantity=period_quantity
            )

            VisualLogger.print_panel(f"BSS GetSubscriptionPrice Payload - [{instance_id}]", request.to_map())

            response = bss_client.get_subscription_price_with_options(request, util_models.RuntimeOptions())
            res_body = response.body.to_map()

            VisualLogger.print_panel(f"BSS Raw Response - [{instance_id}]", res_body)

            is_success = res_body.get('Success')
            req_id = res_body.get('RequestId', 'N/A')

            if is_success:
                data = res_body.get('Data', {})
                trade_price = float(data.get('TradePrice', 0.0))
                original_price = float(data.get('OriginalPrice', 0.0))
                currency = data.get('Currency', 'CNY')
                return trade_price, original_price, currency, specs
            else:
                msg = res_body.get('Message', 'API 询价失败')
                code = res_body.get('Code', '')
                logger.warning(f"❌ [API 拦截透出] 阿里云 BSS 网关拒绝: [{code}] {msg} (RequestId: {req_id})")
                return -1.0, -1.0, f"BSS拒绝: [{code}] {msg}", {}

        except Exception as e:
            logger.error(f"❌ API拒绝: ID: {instance_id} | 技术异常: {str(e)}")
            return -1.0, -1.0, f"技术异常: {str(e)}", {}

    @staticmethod
    def main():
        logger.info("★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★ [STEP: STARTING API QUERY PROCESS] ★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★")
        logger.info("🚀 阿里云路由器接口 (RI) 国际站续费询价工具 (BSS精准计费槽位翻译版)")

        today = datetime.today()
        first_day_of_current = today.replace(day=1)
        last_month_str = (first_day_of_current - timedelta(days=1)).strftime("%Y-%m")
        input_csv_path = os.path.join(DATA_DIR, f'aliyun_renewal_bill_{last_month_str}.csv')

        first_day_of_next_month = (first_day_of_current + timedelta(days=32)).replace(day=1)
        next_month_str = first_day_of_next_month.strftime("%Y-%m")
        current_month_str = today.strftime("%Y-%m")
        output_csv_path = os.path.join(DATA_DIR, f'aliyun_renewal_price_{current_month_str}.csv')

        if not os.path.exists(input_csv_path):
            logger.error(f"❌ 致命阻断: 未找到前置到期数据清单 {input_csv_path}。")
            sys.exit(1)

        session_to_section = AliyunRiRenewalPriceQuery.get_session_name_to_section_map()
        duration_map = AliyunRiRenewalPriceQuery.load_duration_mapping()

        instances_by_account = {}
        total_count = 0
        filtered_count = 0

        with open(input_csv_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                total_count += 1
                instance_id = row.get('资源id', '').strip()
                product_code = row.get('产品代码', '').lower().strip()
                account = row.get('资源所属账号', '').strip()
                expire_time_raw = row.get('资源到期时间', '').strip()

                expire_time_norm = expire_time_raw.replace('/', '-')

                if product_code == 'ri' or instance_id.startswith('ri-'):
                    if account and expire_time_norm.startswith(next_month_str):
                        if account not in instances_by_account:
                            instances_by_account[account] = []
                        instances_by_account[account].append(row)
                        filtered_count += 1

        logger.info(
            f"📊 基础过滤完成：CSV 共扫描 {total_count} 行，筛选出 {filtered_count} 个符合【ri + 下月到期】条件的实例。")

        output_fields = [
            '资源id', '资源所属账号', '资源到期时间', '产品代码',
            '描述', '原价', '折扣', '货币单位', '最终价'
        ]

        file_exists = os.path.exists(output_csv_path)
        with open(output_csv_path, 'a', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=output_fields)
            if not file_exists:
                writer.writeheader()

        total_processed, total_success, total_failed = 0, 0, 0

        for account, items in instances_by_account.items():
            logger.info(f"▶▶ 🏁 账号集群: [{account}] | 待处理RI总数: {len(items)}")
            section = session_to_section.get(account)
            if not section:
                continue

            try:
                credentials = AliyunRiRenewalPriceQuery.get_sts_credentials_by_role(section)
                open_config = open_api_models.Config(
                    access_key_id=credentials.access_key_id,
                    access_key_secret=credentials.access_key_secret,
                    security_token=credentials.security_token
                )

                # 资源型接口：全球路由共用
                open_config.endpoint = 'vpc.aliyuncs.com'
                vpc_client = Vpc20160428Client(open_config)

                # 财务型接口：强制指向国际站物理隔离节点
                open_config.endpoint = 'business.ap-southeast-1.aliyuncs.com'
                bss_client = BssOpenApi20171214Client(open_config)
            except Exception as e:
                logger.error(f"❌ 客户端初始化失败: {str(e)}")
                continue

            processed_rows = []
            for item in items:
                total_processed += 1
                instance_id = item.get('资源id', '').strip()

                duration_val = duration_map.get(instance_id, 1.0)
                period_unit, period_quantity = ('Year', 1) if duration_val == 12.0 else ('Month', (
                    int(duration_val) if duration_val > 0 else 1))

                trade_price, original_price, status_or_currency, applied_specs = AliyunRiRenewalPriceQuery.query_ri_price_via_bss(
                    bss_client, vpc_client, item, period_unit, period_quantity
                )

                out_row = {
                    '资源id': instance_id,
                    '资源所属账号': account,
                    '资源到期时间': item.get('资源到期时间', ''),
                    '产品代码': item.get('产品代码', 'ri'),
                    '描述': '', '原价': '', '折扣': '', '货币单位': '', '最终价': ''
                }

                if trade_price >= 0:
                    total_success += 1
                    out_row['最终价'] = trade_price
                    out_row['原价'] = original_price
                    out_row['货币单位'] = status_or_currency

                    discount = round(original_price - trade_price, 2)
                    out_row['折扣'] = discount if discount > 0 else 0.0

                    spec_desc = f"Bandwidth:{applied_specs.get('Bandwidth', '')}Mbps"
                    out_row['描述'] = spec_desc

                    logger.info(f"✅ 询价成功! ID: {instance_id} | 最终价: {trade_price} {status_or_currency}")
                else:
                    total_failed += 1
                    out_row['描述'] = f"API受阻: {status_or_currency}"

                processed_rows.append(out_row)

            with open(output_csv_path, 'a', encoding='utf-8-sig', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=output_fields)
                writer.writerows(processed_rows)

        logger.info("★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★ [STEP: EXECUTION COMPLETE] ★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★")
        VisualLogger.print_panel("Final Details", {
            "Total Processed": total_processed,
            "Success": total_success,
            "Failed": total_failed,
            "Output Path": output_csv_path,
            "Write Mode": "Append ('a')"
        })


if __name__ == '__main__':
    AliyunRiRenewalPriceQuery.main()