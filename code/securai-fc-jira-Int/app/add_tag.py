import pandas as pd
import os
import logging
from datetime import datetime, timedelta
from typing import Optional

# ===================== 全局配置 =====================
# 路径配置
DATA_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/data'))
LOG_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/log'))
LOG_FILE_NAME = "tag_processing.log"

# 业务配置
TARGET_ACCOUNTS = [
    "itinfra-team@tsi-holdings.com",  # 原有目标资源所属账号
    "zionelement@ali-intl.skyarch.cn",
    "airweave@skyarch.cn"# 新增目标账号
]  # 扩展为列表支持多账号
SPECIAL_RESOURCE_IDS = {
    # 原有特殊资源ID配置
    "a43584cf-4a70-43be-ba38-febbf3b5e0f0": "key:TSI value:infra",
    "ap-northeast-1": "key:TSI value:infra",  # 原有配置（优先级说明：若同一资源ID需适配多账号，见下方优化）
    # 新增特殊规则：zionelement@ali-intl.skyarch.cn 账号下 ap-northeast-1 资源ID的标签
    ("zionelement@ali-intl.skyarch.cn", "ap-northeast-1"): "key:securai value:lacso",
    ("airweave@skyarch.cn", "cn-shanghai"): "key:securai value:awsh",
    ("airweave@skyarch.cn", "cn-hongkong"): "key:securai value:awhk"
}  # 特殊资源ID/（账号,资源ID）对应的标签
DEFAULT_TAG = "key:TSI value:wms"  # 默认标签
# 新增：不同账号的默认标签配置（可选扩展）
ACCOUNT_DEFAULT_TAGS = {
    "itinfra-team@tsi-holdings.com": "key:TSI value:wms",
    "zionelement@ali-intl.skyarch.cn": "key:securai value:default"  # 可自定义新增账号的默认标签
}


# ===================== 日志初始化 =====================
def init_logger() -> logging.Logger:
    """初始化日志系统（输出到文件和控制台）"""
    # 创建日志目录
    os.makedirs(LOG_DIR, exist_ok=True)
    log_file = os.path.join(LOG_DIR, LOG_FILE_NAME)

    # 配置日志格式
    log_format = '%(asctime)s - %(levelname)s - %(message)s'
    handlers = [
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler()
    ]

    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        handlers=handlers
    )

    logger = logging.getLogger(__name__)
    logger.info("=" * 80)
    logger.info(f"🏷️  阿里云账单标签处理工具启动")
    logger.info(f"📅 启动时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"📜 日志文件：{log_file}")
    logger.info("=" * 80)

    return logger


# ===================== 文件处理工具 =====================
def get_last_month_bill_file(logger: logging.Logger) -> Optional[str]:
    """获取上个月的账单文件路径"""
    logger.info("\n📂 开始查找上个月账单文件")

    # 计算上个月年月
    last_month = datetime.now().replace(day=1) - timedelta(days=1)
    file_month = last_month.strftime("%Y-%m")
    file_name = f"aliyun_bill_{file_month}.csv"
    file_path = os.path.join(DATA_DIR, file_name)

    # 检查文件是否存在
    if os.path.exists(file_path):
        logger.info(f"✅ 找到账单文件：{file_path}")
        return file_path
    else:
        logger.error(f"❌ 未找到上个月账单文件：{file_path}")
        return None


# ===================== 数据处理逻辑 =====================
def process_bill_tags(logger: logging.Logger, file_path: str) -> pd.DataFrame:
    """处理账单标签：为符合条件的行添加标签"""
    logger.info(f"\n🔍 开始读取账单文件：{file_path}")

    # 读取CSV文件
    try:
        df = pd.read_csv(file_path, encoding='utf-8-sig')
        logger.info(f"✅ 成功读取文件，共{len(df)}行数据")

        # 检查必要列是否存在
        required_columns = ['标签', '资源所属账号', '资源id']
        missing_cols = [col for col in required_columns if col not in df.columns]
        if missing_cols:
            logger.error(f"❌ 账单文件缺少必要列：{missing_cols}")
            raise ValueError(f"CSV文件缺少列：{missing_cols}")

        # 筛选符合条件的行（标签为空且资源所属账号匹配任一目标账号）
        condition = (df['标签'].isna()) & (df['资源所属账号'].isin(TARGET_ACCOUNTS))
        target_rows = df[condition]
        logger.info(f"📊 筛选结果：标签为空且账号为{TARGET_ACCOUNTS}的行共{len(target_rows)}条")

        if len(target_rows) == 0:
            logger.info("ℹ️  没有需要处理的行，直接返回原数据")
            return df

        # 处理标签逻辑
        logger.info("\n🏷️  开始处理标签数据")
        special_count = 0
        default_count = 0
        account_tag_stats = {}  # 按账号统计标签应用情况

        for idx in target_rows.index:
            resource_id = df.loc[idx, '资源id']
            account = df.loc[idx, '资源所属账号']
            tag_applied = None

            # 初始化账号统计
            if account not in account_tag_stats:
                account_tag_stats[account] = {"special": 0, "default": 0}

            # 优先检查 账号+资源ID 组合的特殊规则
            combined_key = (account, resource_id)
            if combined_key in SPECIAL_RESOURCE_IDS:
                df.loc[idx, '标签'] = SPECIAL_RESOURCE_IDS[combined_key]
                special_count += 1
                account_tag_stats[account]["special"] += 1
                tag_applied = "特殊标签(账号+资源ID组合)"
                logger.debug(f"   - 账号[{account}] 资源ID[{resource_id}]：应用{tag_applied}")
            # 再检查仅资源ID的特殊规则
            elif resource_id in SPECIAL_RESOURCE_IDS:
                df.loc[idx, '标签'] = SPECIAL_RESOURCE_IDS[resource_id]
                special_count += 1
                account_tag_stats[account]["special"] += 1
                tag_applied = "特殊标签(仅资源ID)"
                logger.debug(f"   - 账号[{account}] 资源ID[{resource_id}]：应用{tag_applied}")
            # 应用账号对应的默认标签
            else:
                default_tag = ACCOUNT_DEFAULT_TAGS.get(account, DEFAULT_TAG)
                df.loc[idx, '标签'] = default_tag
                default_count += 1
                account_tag_stats[account]["default"] += 1
                tag_applied = f"默认标签({default_tag})"
                logger.debug(f"   - 账号[{account}] 资源ID[{resource_id}]：应用{tag_applied}")

        # 输出详细统计
        logger.info(f"✅ 标签处理完成：")
        logger.info(f"   - 特殊标签应用数：{special_count}条")
        logger.info(f"   - 默认标签应用数：{default_count}条")
        logger.info(f"   - 总计处理：{special_count + default_count}条")

        # 按账号输出统计
        logger.info(f"\n📈 各账号处理统计：")
        for account, stats in account_tag_stats.items():
            logger.info(f"   - 账号[{account}]：特殊标签{stats['special']}条，默认标签{stats['default']}条")

        return df

    except Exception as e:
        logger.error(f"❌ 读取或处理文件失败：{str(e)}")
        raise


# ===================== 结果保存 =====================
def save_processed_data(logger: logging.Logger, df: pd.DataFrame, original_path: str) -> None:
    """保存处理后的数据"""
    logger.info(f"\n💾 开始保存处理后的数据")

    try:
        # 保存到原文件（或可修改为新文件）
        df.to_csv(original_path, index=False, encoding='utf-8')
        logger.info(f"✅ 数据已保存到：{original_path}")

        # 验证保存结果
        saved_df = pd.read_csv(original_path, encoding='utf-8-sig')
        # 验证所有目标账号的标签处理结果
        for account in TARGET_ACCOUNTS:
            processed_rows = len(saved_df[~saved_df['标签'].isna() & (saved_df['资源所属账号'] == account)])
            logger.info(f"✅ 验证 - 账号[{account}]：处理后共有{processed_rows}条记录包含标签")

    except Exception as e:
        logger.error(f"❌ 保存文件失败：{str(e)}")
        raise


# ===================== 主流程 =====================
def main():
    """程序主流程"""
    logger = init_logger()

    try:
        # 步骤1：获取上个月账单文件
        bill_file = get_last_month_bill_file(logger)
        if not bill_file:
            logger.error("❌ 程序终止：未找到账单文件")
            return

        # 步骤2：处理标签数据
        processed_df = process_bill_tags(logger, bill_file)

        # 步骤3：保存处理结果
        save_processed_data(logger, processed_df, bill_file)

        # 输出最终结果
        logger.info("\n" + "=" * 80)
        logger.info(f"🎉 所有操作完成！")
        logger.info(f"📊 最终统计：")
        logger.info(f"   - 目标账号列表：{TARGET_ACCOUNTS}")
        logger.info(f"   - 特殊资源规则数：{len(SPECIAL_RESOURCE_IDS)}")
        logger.info(f"   - 处理文件：{bill_file}")
        logger.info("=" * 80)

        # 控制台输出
        print("\n" + "=" * 80)
        print(f"🎉 数据处理完成！")
        print(f"📊 处理结果：")
        print(f"   - 账单文件：{bill_file}")
        print(f"   - 目标账号：{TARGET_ACCOUNTS}")
        print(f"   - 特殊规则数：{len(SPECIAL_RESOURCE_IDS)}")
        print(
            f"   - 新增规则：zionelement@ali-intl.skyarch.cn 账号下 ap-northeast-1 资源ID应用标签 key:securai value:lacso")
        print(f"📜 详细日志：{os.path.join(LOG_DIR, LOG_FILE_NAME)}")
        print("=" * 80)

    except Exception as e:
        logger.error(f"❌ 程序执行失败：{str(e)}", exc_info=True)
        print(f"\n❌ 程序执行出错：{str(e)}")
        print(f"📜 请查看日志文件获取详细信息：{os.path.join(LOG_DIR, LOG_FILE_NAME)}")


if __name__ == "__main__":
    main()