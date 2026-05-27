import sys
import time
import logging
import argparse
from datetime import datetime
import pytz
from rich.live import Live
from rich.console import Console

from logger_utils import setup_logger
from market_monitor import MarketMonitor

def main():
    """
    即時行情監控程式的主入口點。
    負責初始化 Logger、啟動行情監控器，並在終端上的同一個位置呈現目前即時的期貨價格和大盤價格。
    """
    # 0. 解析命令列參數
    parser = argparse.ArgumentParser(description="台股期貨與大盤即時監控程式")
    parser.add_argument(
        "-m", "--minimal", 
        action="store_true", 
        help="啟用極簡模式（僅顯示時間和期貨的即時報價，不含表格與邊框）"
    )
    parser.add_argument(
        "-a", "--account",
        action="store_true",
        help="啟用帳戶即時監控模式（僅顯示帳戶保證金水位與持倉部位，不顯示行情）"
    )
    parser.add_argument(
        "-q", "--query",
        action="store_true",
        help="啟用帳戶單次查詢模式（同步向 API 查詢一次帳戶水位與部位後立即結束）"
    )
    args = parser.parse_args()

    # 1. 初始化日誌系統 (日誌寫入至 app.log)
    # 註：這裡將 enable_console 設為 False，防止一般的 print 日誌打亂 Rich Live UI 的動態排版。
    # 所有的 log 將會記錄於 app.log 檔案中，且最近的 4 條 log 會自動被收集並顯示於 UI 最下方的日誌面板中。
    logger = setup_logger(name="SPF_API", log_file="app.log", level=logging.INFO, enable_console=False)
    
    logger.info("=========================================")
    logger.info("台股期貨與大盤即時監控程式啟動中...")

    # 2. 建立行情監控核心物件
    monitor = MarketMonitor(logger)
    
    # 3. 建立 Rich 控制台物件，供 Live 渲染使用
    console = Console()
    
    # 優先處理單次查詢模式
    if args.query:
        try:
            panel = monitor.query_account_once()
            console.print(panel)
        except KeyboardInterrupt:
            logger.warning("偵測到使用者中斷指令...")
        except Exception as e:
            logger.critical(f"單次查詢發生嚴重異常: {e}", exc_info=True)
        finally:
            monitor.stop()
            sys.exit(0)
    
    try:
        # 4. 啟動連線與訂閱服務
        is_account = args.account
        monitor.start(account_only=is_account)
        
        # 5. 使用 Rich Live 進行同位置原地更新呈現
        logger.info("啟動動態終端介面，進入監控主迴圈...")
        
        is_minimal = args.minimal
        # 依據參數決定是否啟用全螢幕模式 (極簡模式不佔用全螢幕，直接在當前行原地更新即可)
        use_screen = not is_minimal
        
        last_version = -1
        last_time_str = ""
        tw_tz = pytz.timezone('Asia/Taipei')

        with Live(monitor.generate_renderable(minimal=is_minimal, account_only=is_account), console=console, screen=use_screen, auto_refresh=False) as live:
            while monitor.running:
                current_version = monitor.state.version
                current_time_str = datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M:%S")

                # 僅在資料版本改變或系統時間秒數變動時重繪畫面
                if current_version != last_version or current_time_str != last_time_str:
                    live.update(monitor.generate_renderable(minimal=is_minimal, account_only=is_account))
                    live.refresh()
                    last_version = current_version
                    last_time_str = current_time_str

                # 以高頻率 (50ms) 檢查，獲得極低的價格變更顯示延遲
                time.sleep(0.05)
                
    except KeyboardInterrupt:
        logger.warning("偵測到使用者中斷指令 (Ctrl+C)...")
    except Exception as e:
        logger.critical(f"主程式發生未預期之嚴重異常: {e}", exc_info=True)
    finally:
        # 6. 安全停止資源釋放與登出
        monitor.stop()
        logger.info("監控程式已安全結束。")
        logger.info("=========================================")
        sys.exit(0)

if __name__ == "__main__":
    main()
