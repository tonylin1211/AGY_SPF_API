import logging
import os
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
import pytz

class TaiwanTimeFormatter(logging.Formatter):
    """
    自訂日誌格式化器 (Formatter)，確保所有日誌的時間戳記均使用台灣時間 (Asia/Taipei)。
    """
    def __init__(self, fmt: str = None, datefmt: str = None, style: str = '%'):
        """
        初始化格式化器，設定台灣時區 (Asia/Taipei)。
        """
        super().__init__(fmt, datefmt, style)
        # 設定台灣時區
        self.tz = pytz.timezone('Asia/Taipei')

    def formatTime(self, record: logging.LogRecord, datefmt: str = None) -> str:
        """
        將日誌記錄的建立時間 (Epoch timestamp) 轉換為台灣時間格式。
        """
        # 將浮點數時間戳記轉換為台灣時區的 datetime 物件
        dt = datetime.fromtimestamp(record.created, self.tz)
        if datefmt:
            return dt.strftime(datefmt)
        else:
            # 預設輸出精確到毫秒及帶有時區偏移的格式，例如: 2026-05-27 11:30:00.123+0800
            return dt.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3] + dt.strftime('%z')

def setup_logger(name: str = "SPF_API", log_file: str = "app.log", level: int = logging.INFO, enable_console: bool = True) -> logging.Logger:
    """
    設定並取得應用程式的 Logger 實例。
    此 Logger 會同時輸出至指定的日誌檔案，並依設定決定是否輸出至控制台。
    
    參數:
        name: Logger 的名稱。
        log_file: 日誌檔案路徑。
        level: 日誌記錄級別。
        enable_console: 是否啟用控制台 (Console) 輸出。
        
    回傳:
        logging.Logger: 設定完成的 Logger 實例。
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # 避免重複添加 Handler
    if logger.handlers:
        return logger

    # 定義標準的日誌格式
    # [時間] [日誌等級] [模組名稱] - 訊息內容
    log_format = '[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s'
    
    # 建立自訂的台灣時間格式化器
    formatter = TaiwanTimeFormatter(log_format)

    # 1. 建立每日輪轉檔案處理器 (TimedRotatingFileHandler) - 每日午夜自動輪轉切換，保留 30 天
    # 確保日誌目錄存在
    log_dir = os.path.dirname(log_file)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir)
        
    file_handler = TimedRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        backupCount=30,
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # 2. 建立控制台處理器 (Console Handler) - 用於即時除錯與終端輸出 (可依參數啟用)
    if enable_console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    return logger
