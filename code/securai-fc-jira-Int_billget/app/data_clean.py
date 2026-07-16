import os
import re
import logging
import shutil
from datetime import datetime
from typing import List, Tuple


def init_logger():
    """初始化日志，输出到 ../../jira/log/data_cleanup.log 和控制台"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.normpath(os.path.join(current_dir, '../../jira/log'))
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'data_cleanup.log')

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file, mode='a', encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)


logger = init_logger()


def get_two_years_ago() -> int:
    """获取两年前的年份（如2026年返回2024）"""
    current_year = datetime.now().year
    two_years_ago = current_year - 2
    return two_years_ago


def delete_log_files() -> Tuple[int, int]:
    """
    删除../../jira/log下的所有文件（跳过当前正在使用的日志文件）

    返回: (删除文件数, 跳过文件数)
    """
    current_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.normpath(os.path.join(current_dir, '../../jira/log'))
    deleted_count = 0
    skipped_count = 0

    if not os.path.exists(log_dir):
        logger.warning(f"⚠️  log目录不存在：{log_dir}，跳过此步骤")
        return 0, 0

    # 获取当前日志文件的绝对路径（用于跳过）
    current_log_path = os.path.normpath(os.path.join(log_dir, 'data_cleanup.log'))

    logger.info(f"开始扫描log目录：{log_dir}")
    for filename in os.listdir(log_dir):
        file_path = os.path.normpath(os.path.join(log_dir, filename))

        # 只处理文件，跳过目录
        if not os.path.isfile(file_path):
            continue

        # 跳过当前正在使用的日志文件
        if file_path == current_log_path:
            skipped_count += 1
            logger.debug(f"跳过当前日志文件：{filename}")
            continue

        # 执行删除
        try:
            os.remove(file_path)
            deleted_count += 1
            logger.info(f"✅ 已删除log文件：{filename}")
        except Exception as e:
            logger.error(f"❌ 删除log文件失败：{filename}，错误：{str(e)}")

    return deleted_count, skipped_count


def delete_old_data_files() -> Tuple[int, int]:
    """
    删除../../jira/data下以年_月.csv结尾的两年前的数据
    如：aliyun_bill_2026-02.csv 不删，aliyun_income_2024-02.csv 删除

    返回: (删除文件数, 跳过文件数)
    """
    current_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.normpath(os.path.join(current_dir, '../../jira/data'))
    two_years_ago = get_two_years_ago()
    deleted_count = 0
    skipped_count = 0

    if not os.path.exists(data_dir):
        logger.warning(f"⚠️  data目录不存在：{data_dir}，跳过此步骤")
        return 0, 0

    # 匹配文件名格式：*_YYYY-MM.csv（如 aliyun_bill_2024-02.csv）
    pattern = re.compile(r'.*_(\d{4})-\d{2}\.csv$')

    logger.info(f"开始扫描data目录：{data_dir}，清理{two_years_ago}年及更早的年月格式CSV")
    for filename in os.listdir(data_dir):
        file_path = os.path.normpath(os.path.join(data_dir, filename))

        # 只处理文件，跳过目录
        if not os.path.isfile(file_path):
            continue

        # 匹配文件名格式
        match = pattern.match(filename)
        if not match:
            skipped_count += 1
            logger.debug(f"跳过非目标格式文件：{filename}")
            continue

        # 提取年份并判断
        file_year = int(match.group(1))
        if file_year <= two_years_ago:
            # 执行删除
            try:
                os.remove(file_path)
                deleted_count += 1
                logger.info(f"✅ 已删除旧数据文件：{filename}（年份：{file_year}）")
            except Exception as e:
                logger.error(f"❌ 删除旧数据文件失败：{filename}，错误：{str(e)}")
        else:
            skipped_count += 1
            logger.debug(f"跳过近期数据文件：{filename}（年份：{file_year}）")

    return deleted_count, skipped_count


def delete_monthly_report_files() -> Tuple[int, int]:
    """
    删除../../jira/monthly_report下除了"课金月报模板"开头的其余所有文件及文件夹

    返回: (删除文件数, 跳过文件数)
    """
    current_dir = os.path.dirname(os.path.abspath(__file__))
    report_dir = os.path.normpath(os.path.join(current_dir, '../../jira/monthly_report'))
    deleted_count = 0
    skipped_count = 0

    if not os.path.exists(report_dir):
        logger.warning(f"⚠️  monthly_report目录不存在：{report_dir}，跳过此步骤")
        return 0, 0

    logger.info(f"开始扫描monthly_report目录：{report_dir}，保留'课金月报模板'开头的文件及文件夹")
    for filename in os.listdir(report_dir):
        file_path = os.path.normpath(os.path.join(report_dir, filename))

        # 判断是否以"课金月报模板"开头
        if filename.startswith('课金月报模板'):
            skipped_count += 1
            logger.debug(f"跳过保留项：{filename}")
            continue

        # 执行删除（区分文件和文件夹）
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
                deleted_count += 1
                logger.info(f"✅ 已删除月报文件：{filename}")
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path)
                deleted_count += 1
                logger.info(f"✅ 已删除月报文件夹：{filename}")
        except Exception as e:
            logger.error(f"❌ 删除月报项失败：{filename}，错误：{str(e)}")

    return deleted_count, skipped_count


def main():
    # 程序启动日志
    start_time = datetime.now()
    logger.info("=" * 80)
    logger.info(f"数据清理任务启动（时间：{start_time.strftime('%Y-%m-%d %H:%M:%S')}）")
    logger.info(f"两年前年份：{get_two_years_ago()}（该年份及更早的年月格式CSV将被删除）")
    logger.info("=" * 80)

    # 控制台友好提示
    print("\n" + "=" * 80)
    print("开始执行数据清理任务")
    print("=" * 80)
    print(f"清理规则：")
    print(f"  1. 删除../../jira/log下所有文件（跳过当前日志）")
    print(f"  2. 删除../../jira/data下{get_two_years_ago()}年及更早的年月格式CSV")
    print(f"  3. 删除../../jira/monthly_report下除'课金月报模板'开头的所有文件及文件夹")
    print("=" * 80)

    # 总统计
    total_deleted = 0
    total_skipped = 0

    # 【步骤1/3】清理log目录
    logger.info(f"\n【步骤1/3】清理../../jira/log目录")
    print(f"\n【1/3】开始清理log目录...")
    try:
        log_deleted, log_skipped = delete_log_files()
        total_deleted += log_deleted
        total_skipped += log_skipped
        logger.info(f"✅ log目录清理完成：删除{log_deleted}个，跳过{log_skipped}个")
        print(f"【1/3】log目录清理完成：删除{log_deleted}个，跳过{log_skipped}个")
    except Exception as e:
        logger.error(f"❌ log目录清理异常：{str(e)}", exc_info=True)
        print(f"【1/3】log目录清理异常：{str(e)}")

    # 【步骤2/3】清理data目录旧数据
    logger.info(f"\n【步骤2/3】清理../../jira/data目录旧数据")
    print(f"\n【2/3】开始清理data目录旧数据...")
    try:
        data_deleted, data_skipped = delete_old_data_files()
        total_deleted += data_deleted
        total_skipped += data_skipped
        logger.info(f"✅ data目录清理完成：删除{data_deleted}个，跳过{data_skipped}个")
        print(f"【2/3】data目录清理完成：删除{data_deleted}个，跳过{data_skipped}个")
    except Exception as e:
        logger.error(f"❌ data目录清理异常：{str(e)}", exc_info=True)
        print(f"【2/3】data目录清理异常：{str(e)}")

    # 【步骤3/3】清理monthly_report目录
    logger.info(f"\n【步骤3/3】清理../../jira/monthly_report目录")
    print(f"\n【3/3】开始清理monthly_report目录...")
    try:
        report_deleted, report_skipped = delete_monthly_report_files()
        total_deleted += report_deleted
        total_skipped += report_skipped
        logger.info(f"✅ monthly_report目录清理完成：删除{report_deleted}个，跳过{report_skipped}个")
        print(f"【3/3】monthly_report目录清理完成：删除{report_deleted}个，跳过{report_skipped}个")
    except Exception as e:
        logger.error(f"❌ monthly_report目录清理异常：{str(e)}", exc_info=True)
        print(f"【3/3】monthly_report目录清理异常：{str(e)}")

    # 任务结束统计
    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()
    logger.info(f"\n" + "=" * 80)
    logger.info(f"数据清理任务全部完成")
    logger.info(f"  - 任务耗时：{duration:.2f} 秒")
    logger.info(f"  - 总删除文件数：{total_deleted}")
    logger.info(f"  - 总跳过文件数：{total_skipped}")
    logger.info("=" * 80)
    logger.info(f"任务结束（时间：{end_time.strftime('%Y-%m-%d %H:%M:%S')}）")
    logger.info("=" * 80 + "\n")

    # 控制台最终汇总
    print("\n" + "=" * 80)
    print("数据清理任务完成！")
    print("=" * 80)
    print(f"任务耗时：{duration:.2f} 秒")
    print(f"总删除文件数：{total_deleted}")
    print(f"总跳过文件数：{total_skipped}")
    print("=" * 80)


if __name__ == "__main__":
    main()