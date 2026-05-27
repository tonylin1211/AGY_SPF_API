import time
import threading
import collections
import logging
from dataclasses import dataclass
from datetime import datetime
import pytz
from rich import box
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.console import Group
import shioaji as sj

from config import Config

@dataclass
class MarketState:
    """
    用來儲存期貨與大盤即時報價狀態的資料結構 (Thread-safe)
    """
    # 大盤指數 (Index) 報價欄位
    index_price: float = 0.0          # 當前成交價
    index_change: float = 0.0         # 漲跌點數
    index_change_pct: float = 0.0     # 漲跌幅度 (%)
    index_high: float = 0.0           # 當日最高
    index_low: float = 0.0            # 當日最低
    index_open: float = 0.0           # 當日開盤
    index_ref: float = 0.0            # 昨收參考價
    index_time: str = "-"             # 最後更新時間

    # 期貨 (Futures) 報價欄位
    futures_price: float = 0.0        # 當前成交價
    futures_change: float = 0.0       # 漲跌點數
    futures_change_pct: float = 0.0   # 漲跌幅度 (%)
    futures_volume: int = 0           # 單筆成交量
    futures_total_volume: int = 0     # 當日累計成交量
    futures_high: float = 0.0         # 當日最高
    futures_low: float = 0.0          # 當日最低
    futures_open: float = 0.0         # 當日開盤
    futures_ref: float = 0.0          # 昨收參考價
    futures_time: str = "-"           # 最後更新時間

    # 期貨帳戶資金與保證金水位欄位
    yesterday_balance: float = 0.0     # 昨日餘額
    today_balance: float = 0.0         # 今日餘額
    equity: float = 0.0                # 帳戶權益總值
    available_margin: float = 0.0      # 可用保證金 (可動用餘額)
    maintenance_margin: float = 0.0    # 維持保證金門檻
    risk_indicator: float = 0.0        # 風險指標 (%)
    futures_pnl: float = 0.0           # 期貨未平倉部位總損益
    
    # 未平倉部位列表 (元素為字典或 PositionInfo 結構)
    positions: list = None
    account_error: str = None          # 儲存帳戶與部位查詢時的錯誤訊息 (例如 406 錯誤)

    # 系統運行狀態
    status: str = "初始化中..."
    mode: str = "SinoPac API"          # 運行模式說明 (實體 API 或是 Mock 模擬)
    version: int = 0                   # 資料更新版本號 (用以檢測是否有新 Tick 變更)
    
    # 執行緒鎖，確保多執行緒存取資料時的安全
    lock: threading.Lock = threading.Lock()

class LogCollectorHandler(logging.Handler):
    """
    自訂的 Logging Handler，用於在記憶體中收集最近幾條 log，
    以便直接顯示在終端 UI 的 Panel 區塊中。
    """
    def __init__(self, max_items: int = 5):
        super().__init__()
        self.log_queue = collections.deque(maxlen=max_items)
        self.tz = pytz.timezone('Asia/Taipei')

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            self.log_queue.append(msg)
        except Exception:
            self.handleError(record)

class MarketMonitor:
    """
    即時行情監控與帳戶部位查詢核心類別，負責：
    1. 永豐 API 連線與訂閱。
    2. 即時價格回呼處理。
    3. 期貨帳戶保證金與部位定期查詢。
    4. Mock 模擬模式的退避、行情與持倉損益計算。
    5. 提供終端 UI 渲染內容。
    """
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.state = MarketState()
        self.state.positions = []
        self.running = False
        
        # 初始化 Shioaji API 物件
        self.api = None
        
        # 註冊記憶體日誌收集器，以便在 UI 呈現最近的日誌
        self.log_collector = LogCollectorHandler(max_items=4)
        log_format = '[%(asctime)s] [%(levelname)s] %(message)s'
        from logger_utils import TaiwanTimeFormatter
        self.log_collector.setFormatter(TaiwanTimeFormatter(log_format, datefmt="%H:%M:%S"))
        self.logger.addHandler(self.log_collector)
        
        # 執行緒參考
        self._mock_thread = None
        self._account_thread = None

    def start(self, account_only: bool = False):
        """
        啟動監控服務。
        嘗試登入永豐 API，若未提供金鑰或登入失敗，會自動切換為 Mock 模式。
        """
        self.running = True
        
        if not Config.has_credentials():
            self.logger.warning("未偵測到 SHIOAJI_API_KEY 或 SHIOAJI_SECRET_KEY 變數，將進入 Mock 模擬演示模式。")
            self.state.mode = "Mock 帳戶模式" if account_only else "Mock 模擬模式"
            self._start_mock_mode()
            return

        try:
            self.logger.info(f"正在連線至永豐期貨 API (Simulation={Config.SIMULATION})...")
            self.api = sj.Shioaji(simulation=Config.SIMULATION)
            
            # 執行登入
            self.logger.info("進行 API 金鑰驗證...")
            self.api.login(api_key=Config.API_KEY, secret_key=Config.SECRET_KEY)
            self.logger.info("永豐 API 驗證登入成功！")
            
            if not account_only:
                # 下載與初始化合約資訊
                self.logger.info("正在初始化市場商品合約資訊...")
                futures_contract = self._get_futures_contract()
                index_contract = self._get_index_contract()
                
                if not futures_contract or not index_contract:
                    raise ValueError("無法成功取得期貨或指數合約物件，可能 API 尚未完成合約下載。")
                    
                # 初始化昨收價與快照資訊
                self._initialize_snapshots(futures_contract, index_contract)
                
                # 設定訂閱回呼 (Callback) - 註冊專屬的 Tick 回呼，覆蓋 Shioaji 預設在 stdout 的 print 行為
                self.logger.info("註冊報價更新回呼事件...")
                self.api.set_on_quote_callback(self._on_quote_callback)
                self.api.set_on_tick_fop_v1_callback(self._on_tick_fop_callback)
                
                # 訂閱即時 Tick 報價
                self.logger.info(f"開始訂閱期貨合約: {Config.FUTURES_CODE}...")
                self.api.quote.subscribe(futures_contract, quote_type='tick')
                
                self.logger.info(f"開始訂閱加權指數: {Config.INDEX_CODE}...")
                self.api.quote.subscribe(index_contract, quote_type='tick')
                
                self.state.mode = "SinoPac API 實時"
                self.logger.info("API 行情訂閱設定完成。")
            else:
                self.state.mode = "SinoPac API 帳戶"
                self.logger.info("帳戶監控模式啟動，跳過行情訂閱。")
            
            # 啟動定期查詢帳務保證金與持倉部位的背景執行緒
            self._account_thread = threading.Thread(target=self._run_account_query_loop, daemon=True)
            self._account_thread.start()
            
        except Exception as e:
            self.logger.error(f"永豐 API 初始化或連線失敗: {e}")
            self.logger.warning("正在自動降級切換至 Mock 模擬演示模式...")
            self.state.mode = "Mock 帳戶模式" if account_only else "Mock 模擬模式"
            self._start_mock_mode()

    def _get_futures_contract(self):
        """
        取得期貨合約物件，包含容錯處理。
        """
        try:
            return self.api.Contracts.Futures.TXF[Config.FUTURES_CODE]
        except Exception as e:
            self.logger.warning(f"取得期貨合約屬性失敗 ({e})，嘗試替代查詢法...")
            try:
                return getattr(self.api.Contracts.Futures.TXF, Config.FUTURES_CODE)
            except Exception as ex:
                self.logger.error(f"無法取得期貨合約 TXFR1: {ex}")
                return None

    def _get_index_contract(self):
        """
        取得指數合約物件，包含容錯處理。
        """
        try:
            return self.api.Contracts.Indexs.TSE[Config.INDEX_CODE]
        except Exception as e:
            self.logger.warning(f"取得指數合約屬性失敗 ({e})，嘗試替代查詢法...")
            try:
                return getattr(self.api.Contracts.Indexs.TSE, Config.INDEX_CODE)
            except Exception as ex:
                self.logger.error(f"無法取得指數合約 TSE001: {ex}")
                return None

    def _initialize_snapshots(self, futures_contract, index_contract):
        """
        使用 Snapshot (快照) 初始化商品的基本資訊，即使在非開盤時間也能有初始數據。
        """
        with self.state.lock:
            self.state.futures_ref = float(getattr(futures_contract, 'reference', 0.0))
            self.state.futures_price = self.state.futures_ref
            self.state.index_ref = float(getattr(index_contract, 'reference', 0.0))
            self.state.index_price = self.state.index_ref

        try:
            self.logger.info("向永豐伺服器請求市場快照以初始化報價狀態...")
            snaps = self.api.snapshots([futures_contract, index_contract])
            if snaps:
                for snap in snaps:
                    code = getattr(snap, 'code', '')
                    close_val = float(getattr(snap, 'close', 0.0) or 0.0)
                    ref_val = float(getattr(snap, 'reference', 0.0) or 0.0)
                    open_val = float(getattr(snap, 'open', 0.0) or 0.0)
                    high_val = float(getattr(snap, 'high', 0.0) or 0.0)
                    low_val = float(getattr(snap, 'low', 0.0) or 0.0)
                    
                    with self.state.lock:
                        if code == Config.FUTURES_CODE:
                            self.state.futures_ref = ref_val or self.state.futures_ref
                            self.state.futures_price = close_val or self.state.futures_price
                            self.state.futures_open = open_val
                            self.state.futures_high = high_val
                            self.state.futures_low = low_val
                            self.state.futures_total_volume = int(getattr(snap, 'total_volume', 0) or 0)
                            
                            if self.state.futures_ref > 0:
                                self.state.futures_change = self.state.futures_price - self.state.futures_ref
                                self.state.futures_change_pct = (self.state.futures_change / self.state.futures_ref) * 100
                            self.state.futures_time = datetime.now(pytz.timezone('Asia/Taipei')).strftime("%H:%M:%S")
                            
                        elif code == Config.INDEX_CODE:
                            self.state.index_ref = ref_val or self.state.index_ref
                            self.state.index_price = close_val or self.state.index_price
                            self.state.index_open = open_val
                            self.state.index_high = high_val
                            self.state.index_low = low_val
                            
                            if self.state.index_ref > 0:
                                self.state.index_change = self.state.index_price - self.state.index_ref
                                self.state.index_change_pct = (self.state.index_change / self.state.index_ref) * 100
                            self.state.index_time = datetime.now(pytz.timezone('Asia/Taipei')).strftime("%H:%M:%S")
                            
                self.logger.info("市場快照資料初始化成功。")
        except Exception as e:
            self.logger.warning(f"請求商品快照失敗 (若在非交易時段為正常現象): {e}")

    def _safe_get(self, obj, key, default=None):
        """
        安全的欄位取值輔助函式，支援字典格式與物件屬性獲取，並忽略大小寫。
        """
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k.lower() == key.lower():
                    return v
            return default
        else:
            for attr in dir(obj):
                if attr.lower() == key.lower():
                    return getattr(obj, attr)
            return default

    def _on_tick_fop_callback(self, tick):
        """
        期貨 Tick 報價更新回呼
        """
        try:
            self._process_tick_data(tick, is_futures=True, is_index=False)
        except Exception as e:
            self.logger.error(f"處理期貨 Tick 時發生異常: {e}")

    def _on_quote_callback(self, *args, **kwargs):
        """
        處理大盤指數等通用報價的統一進入點，相容不同版本的參數設計，防止預設列印。
        """
        try:
            topic = ""
            quote = None
            
            if len(args) == 2:
                topic, quote = args
            elif len(args) == 1:
                quote = args[0]
                topic = self._safe_get(quote, 'code') or ""
                
            if quote:
                is_index = "001" in str(topic) or "001" in str(self._safe_get(quote, 'code'))
                if is_index:
                    self._process_tick_data(quote, is_futures=False, is_index=True)
        except Exception as e:
            self.logger.error(f"處理通用報價回呼時發生異常: {e}")

    def _process_tick_data(self, tick, is_futures: bool, is_index: bool):
        """
        處理與解析來自 Shioaji API 的實時 Tick 數據，寫入至狀態物件中。
        """
        close_val = self._safe_get(tick, 'close')
        if close_val is None:
            return
            
        close_price = float(close_val)
        high_val = self._safe_get(tick, 'high')
        low_val = self._safe_get(tick, 'low')
        open_val = self._safe_get(tick, 'open')
        
        time_val = self._safe_get(tick, 'time') or self._safe_get(tick, 'datetime')
        if time_val:
            if isinstance(time_val, datetime):
                tw_tz = pytz.timezone('Asia/Taipei')
                time_str = time_val.astimezone(tw_tz).strftime("%H:%M:%S")
            else:
                time_str = str(time_val).split()[-1][:8]
        else:
            time_str = datetime.now(pytz.timezone('Asia/Taipei')).strftime("%H:%M:%S")

        with self.state.lock:
            if is_index:
                self.state.index_price = close_price
                if open_val is not None: self.state.index_open = float(open_val)
                if high_val is not None: self.state.index_high = float(high_val)
                if low_val is not None: self.state.index_low = float(low_val)
                
                if self.state.index_ref > 0:
                    self.state.index_change = self.state.index_price - self.state.index_ref
                    self.state.index_change_pct = (self.state.index_change / self.state.index_ref) * 100
                self.state.index_time = time_str
                
            elif is_futures:
                self.state.futures_price = close_price
                if open_val is not None: self.state.futures_open = float(open_val)
                if high_val is not None: self.state.futures_high = float(high_val)
                if low_val is not None: self.state.futures_low = float(low_val)
                
                vol = self._safe_get(tick, 'volume')
                tot_vol = self._safe_get(tick, 'vol_sum') or self._safe_get(tick, 'total_volume')
                if vol is not None: self.state.futures_volume = int(vol)
                if tot_vol is not None: self.state.futures_total_volume = int(tot_vol)
                
                if self.state.futures_ref > 0:
                    self.state.futures_change = self.state.futures_price - self.state.futures_ref
                    self.state.futures_change_pct = (self.state.futures_change / self.state.futures_ref) * 100
                self.state.futures_time = time_str
            
            # 更新版本號以觸發畫面重新渲染
            self.state.version += 1

    def _run_account_query_loop(self):
        """
        每 10 秒定期向伺服器查詢帳戶保證金與持倉部位，避免觸發 API 頻率限制。
        """
        self.logger.info("啟動帳戶與部位定期查詢服務...")
        while self.running:
            if self.api and self.state.mode in ("SinoPac API 實時", "SinoPac API 帳戶"):
                # 確保帳戶對象存在
                account = getattr(self.api, 'futopt_account', None)
                if account:
                    try:
                        self.logger.debug("發送 API 帳戶保證金查詢...")
                        margin = self.api.margin(account)
                        
                        self.logger.debug("發送 API 持倉部位查詢...")
                        positions_raw = self.api.list_positions(account)
                        
                        with self.state.lock:
                            self.state.account_error = None
                            if margin:
                                self.state.yesterday_balance = float(getattr(margin, 'yesterday_balance', 0.0) or 0.0)
                                self.state.today_balance = float(getattr(margin, 'today_balance', 0.0) or 0.0)
                                self.state.equity = float(getattr(margin, 'equity', 0.0) or 0.0)
                                self.state.available_margin = float(getattr(margin, 'available_margin', 0.0) or 0.0)
                                self.state.maintenance_margin = float(getattr(margin, 'maintenance_margin', 0.0) or 0.0)
                                self.state.risk_indicator = float(getattr(margin, 'risk_indicator', 0.0) or 0.0)
                                self.state.futures_pnl = float(getattr(margin, 'future_open_position', 0.0) or 0.0)
                                
                            new_positions = []
                            if positions_raw:
                                for pos in positions_raw:
                                    dir_val = getattr(pos, 'direction', 'Buy')
                                    dir_str = "多" if "Buy" in str(dir_val) else "空"
                                    
                                    new_positions.append({
                                        'code': getattr(pos, 'code', ''),
                                        'direction': dir_str,
                                        'quantity': int(getattr(pos, 'quantity', 0)),
                                        'price': float(getattr(pos, 'price', 0.0)),
                                        'pnl': float(getattr(pos, 'pnl', 0.0))
                                    })
                            self.state.positions = new_positions
                            self.state.version += 1
                            
                    except Exception as e:
                        err_msg = str(e)
                        self.logger.error(f"查詢期貨帳戶帳務資訊時發生錯誤: {err_msg}")
                        with self.state.lock:
                            if "406" in err_msg or "Account Not Acceptable" in err_msg:
                                self.state.account_error = "查詢失敗 (錯誤碼 406): 請登入永豐期貨官網簽署「API電子交易風險預告書暨使用同意書」"
                            else:
                                self.state.account_error = f"查詢失敗: {err_msg}"
                            self.state.version += 1
                else:
                    self.logger.warning("查無有效的期貨選擇權交易帳戶 (futopt_account)")
                    
            # 阻斷型休眠：每 0.5 秒檢測一次 running，加速退出
            for _ in range(20):
                if not self.running:
                    break
                time.sleep(0.5)

    def _start_mock_mode(self):
        """
        初始化並啟動 Mock 模擬數據生成背景執行緒。
        """
        self._mock_thread = threading.Thread(target=self._run_mock_simulator, daemon=True)
        self._mock_thread.start()

    def _run_mock_simulator(self):
        """
        模擬行情走勢的背景執行緒邏輯
        """
        import random
        self.logger.info("啟動 Mock 模擬行情產生器...")
        
        with self.state.lock:
            self.state.mode = "Mock 模擬模式"
            self.state.index_ref = 21500.00
            self.state.index_price = 21500.00
            self.state.index_open = 21500.00
            self.state.index_high = 21500.00
            self.state.index_low = 21500.00
            
            self.state.futures_ref = 21480.00
            self.state.futures_price = 21480.00
            self.state.futures_open = 21480.00
            self.state.futures_high = 21480.00
            self.state.futures_low = 21480.00
            self.state.futures_total_volume = 12450
            
            # 初始化模擬帳戶與部位 (建倉 1 口 TXFR1 多單，成本 21450)
            self.state.equity = 1500000.00
            self.state.available_margin = 1290000.00
            self.state.maintenance_margin = 150000.00
            self.state.risk_indicator = 86.50
            self.state.positions = [{
                'code': Config.FUTURES_CODE,
                'direction': "多",
                'quantity': 1,
                'price': 21450.00,
                'pnl': 0.0
            }]
            
            tw_tz = pytz.timezone('Asia/Taipei')
            init_time = datetime.now(tw_tz).strftime("%H:%M:%S")
            self.state.index_time = init_time
            self.state.futures_time = init_time

        while self.running:
            time.sleep(random.uniform(0.5, 1.5))
            
            index_tick = random.choice([-2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0]) * random.uniform(0.5, 2.0)
            futures_tick = index_tick + random.choice([-1.5, -0.5, 0.0, 0.5, 1.5])
            
            index_tick = max(min(index_tick, 10.0), -10.0)
            futures_tick = max(min(futures_tick, 15.0), -15.0)
            
            tw_tz = pytz.timezone('Asia/Taipei')
            now_str = datetime.now(tw_tz).strftime("%H:%M:%S")

            with self.state.lock:
                self.state.index_price = round(self.state.index_price + index_tick, 2)
                self.state.index_high = max(self.state.index_high, self.state.index_price)
                self.state.index_low = min(self.state.index_low, self.state.index_price)
                self.state.index_change = round(self.state.index_price - self.state.index_ref, 2)
                self.state.index_change_pct = round((self.state.index_change / self.state.index_ref) * 100, 2)
                self.state.index_time = now_str
                
                self.state.futures_price = round(self.state.futures_price + futures_tick, 0)
                self.state.futures_high = max(self.state.futures_high, self.state.futures_price)
                self.state.futures_low = min(self.state.futures_low, self.state.futures_price)
                self.state.futures_change = round(self.state.futures_price - self.state.futures_ref, 0)
                self.state.futures_change_pct = round((self.state.futures_change / self.state.futures_ref) * 100, 2)
                
                vol = random.randint(1, 20)
                self.state.futures_volume = vol
                self.state.futures_total_volume += vol
                self.state.futures_time = now_str
                
                # 計算模擬持倉部位的損益 (台指期 1 點為 200 元新台幣)
                if self.state.positions:
                    for pos in self.state.positions:
                        if pos['code'] == Config.FUTURES_CODE:
                            entry_price = pos['price']
                            current_price = self.state.futures_price
                            pnl_points = (current_price - entry_price) if pos['direction'] == "多" else (entry_price - current_price)
                            pos['pnl'] = round(pnl_points * 200 * pos['quantity'], 0)
                            
                            # 連動更新帳戶資金狀態
                            self.state.futures_pnl = pos['pnl']
                            self.state.equity = 1500000.00 + pos['pnl']
                            self.state.available_margin = 1290000.00 + pos['pnl']
                            self.state.risk_indicator = round(max(min(86.50 + (pos['pnl'] / 60000.0), 100.0), 15.0), 2)

                self.state.version += 1
                self.logger.debug(f"Mock 數據生成: 大盤={self.state.index_price} ({self.state.index_change}), 期貨={self.state.futures_price} ({self.state.futures_change})")

    def generate_renderable(self, minimal: bool = False, account_only: bool = False):
        """
        生成適合 Rich Live 渲染的組件。
        """
        if minimal:
            return self.generate_minimal_renderable()

        tw_tz = pytz.timezone('Asia/Taipei')
        now_str = datetime.now(tw_tz).strftime("%Y-%m-%d %H:%M:%S")
        
        if account_only:
            header_text = Text()
            header_text.append("📊 台股期貨帳戶與持倉監控 (SPF Account)", style="bold yellow")
            header_text.append(" | ", style="white")
            header_text.append(f"運行模式: {self.state.mode}", style="bold cyan")
            header_text.append(" | ", style="white")
            header_text.append(f"台灣時間: {now_str}", style="bold green")
            
            account_panel = self._generate_account_panel()
            
            recent_logs = list(self.log_collector.log_queue)
            log_text = "\n".join(recent_logs) if recent_logs else "正在初始化系統日誌..."
            log_panel = Panel(
                Text(log_text, style="dim white"), 
                title="⚡ 系統即時日誌 (記錄檔: app.log)", 
                border_style="dim white",
                height=7
            )
            
            return Group(
                Panel(header_text, border_style="yellow"),
                account_panel,
                log_panel
            )

        header_text = Text()
        header_text.append("📊 台股即時行情監控系統 (SPF Monitor)", style="bold cyan")
        header_text.append(" | ", style="white")
        header_text.append(f"運行模式: {self.state.mode}", style="bold yellow")
        header_text.append(" | ", style="white")
        header_text.append(f"台灣時間: {now_str}", style="bold green")
        
        table = Table(box=box.ROUNDED, show_header=True, header_style="bold magenta", expand=True)
        table.add_column("商品名稱", justify="center", style="bold white")
        table.add_column("商品代碼", justify="center")
        table.add_column("當前成交價", justify="right")
        table.add_column("漲跌點數", justify="right")
        table.add_column("漲跌幅", justify="right")
        table.add_column("開盤價", justify="right")
        table.add_column("最高價", justify="right")
        table.add_column("最低價", justify="right")
        table.add_column("單筆量", justify="right")
        table.add_column("累積總量", justify="right")
        table.add_column("最後更新時間", justify="center")
        
        with self.state.lock:
            idx_price = f"{self.state.index_price:.2f}"
            if self.state.index_change > 0:
                idx_change = f"+{self.state.index_change:.2f}"
                idx_pct = f"+{self.state.index_change_pct:.2f}%"
                idx_style = "bold red"
            elif self.state.index_change < 0:
                idx_change = f"{self.state.index_change:.2f}"
                idx_pct = f"{self.state.index_change_pct:.2f}%"
                idx_style = "bold green"
            else:
                idx_change = "0.00"
                idx_pct = "0.00%"
                idx_style = "white"

            table.add_row(
                "加權指數 (大盤)",
                Config.INDEX_CODE,
                Text(idx_price, style=idx_style),
                Text(idx_change, style=idx_style),
                Text(idx_pct, style=idx_style),
                f"{self.state.index_open:.2f}",
                f"{self.state.index_high:.2f}",
                f"{self.state.index_low:.2f}",
                "-",
                "-",
                self.state.index_time
            )
            
            fut_price = f"{self.state.futures_price:.0f}"
            if self.state.futures_change > 0:
                fut_change = f"+{self.state.futures_change:.0f}"
                fut_pct = f"+{self.state.futures_change_pct:.2f}%"
                fut_style = "bold red"
            elif self.state.futures_change < 0:
                fut_change = f"{self.state.futures_change:.0f}"
                fut_pct = f"{self.state.futures_change_pct:.2f}%"
                fut_style = "bold green"
            else:
                fut_change = "0"
                fut_pct = "0.00%"
                fut_style = "white"

            table.add_row(
                "台指期貨近月",
                Config.FUTURES_CODE,
                Text(fut_price, style=fut_style),
                Text(fut_change, style=fut_style),
                Text(fut_pct, style=fut_style),
                f"{self.state.futures_open:.0f}",
                f"{self.state.futures_high:.0f}",
                f"{self.state.futures_low:.0f}",
                f"{self.state.futures_volume}",
                f"{self.state.futures_total_volume}",
                self.state.futures_time
            )

        # 3. 獲取帳戶部位的面板
        account_panel = self._generate_account_panel()

        # 4. 底部日誌面板
        recent_logs = list(self.log_collector.log_queue)
        log_text = "\n".join(recent_logs) if recent_logs else "正在初始化系統日誌..."
        log_panel = Panel(
            Text(log_text, style="dim white"), 
            title="⚡ 系統即時日誌 (記錄檔: app.log)", 
            border_style="dim white",
            height=7
        )
        
        return Group(
            Panel(header_text, border_style="cyan"),
            table,
            account_panel,
            log_panel
        )

    def _generate_account_panel(self) -> Panel:
        """
        生成帳戶保證金與部位持倉的 Rich Panel。
        """
        with self.state.lock:
            # 1. 格式化保證金資訊列
            equity_str = f"{self.state.equity:,.0f}"
            avail_str = f"{self.state.available_margin:,.0f}"
            pnl_val = self.state.futures_pnl
            
            if pnl_val > 0:
                pnl_str = f"+{pnl_val:,.0f}"
                pnl_style = "bold red"
            elif pnl_val < 0:
                pnl_str = f"{pnl_val:,.0f}"
                pnl_style = "bold green"
            else:
                pnl_str = "0"
                pnl_style = "white"
                
            risk_str = f"{self.state.risk_indicator:.2f}%"
            if self.state.risk_indicator < 60.0 and self.state.risk_indicator > 0.0:
                risk_style = "bold blink red"  # 風險係數過低警告
            elif self.state.risk_indicator < 80.0 and self.state.risk_indicator > 0.0:
                risk_style = "bold yellow"
            else:
                risk_style = "bold green"
                
            info_text = Text()
            info_text.append("權益總值: ", style="bold white")
            info_text.append(f"{equity_str} 元", style="cyan")
            info_text.append(" | ")
            info_text.append("可用保證金: ", style="bold white")
            info_text.append(f"{avail_str} 元", style="cyan")
            info_text.append(" | ")
            info_text.append("期貨部位未平倉損益: ", style="bold white")
            info_text.append(f"{pnl_str} 元", style=pnl_style)
            info_text.append(" | ")
            info_text.append("風險指標: ", style="bold white")
            info_text.append(risk_str, style=risk_style)

            # 2. 建立部位持倉表格
            pos_table = Table(box=box.SIMPLE, show_header=True, header_style="bold yellow", expand=True)
            pos_table.add_column("商品代碼", justify="center")
            pos_table.add_column("方向", justify="center")
            pos_table.add_column("持倉口數", justify="right")
            pos_table.add_column("進場均價", justify="right")
            pos_table.add_column("當前估價", justify="right")
            pos_table.add_column("估算損益", justify="right")
            
            if self.state.positions:
                for pos in self.state.positions:
                    code = pos.get('code', '')
                    direction = pos.get('direction', '多')
                    dir_style = "bold red" if direction == "多" else "bold green"
                    qty = pos.get('quantity', 0)
                    cost = pos.get('price', 0.0)
                    pnl = pos.get('pnl', 0.0)
                    
                    # 抓取對應商品的當前價格
                    curr_price = "-"
                    if code == Config.FUTURES_CODE:
                        curr_price = f"{self.state.futures_price:.0f}"
                        
                    if pnl > 0:
                        pnl_item = Text(f"+{pnl:,.0f} 元", style="bold red")
                    elif pnl < 0:
                        pnl_item = Text(f"{pnl:,.0f} 元", style="bold green")
                    else:
                        pnl_item = Text("0 元", style="white")
                        
                    pos_table.add_row(
                        code,
                        Text(direction, style=dir_style),
                        f"{qty} 口",
                        f"{cost:,.0f}",
                        curr_price,
                        pnl_item
                    )
            else:
                pos_table.add_row("-", "-", "-", "-", "-", "無未平倉部位")
                
            has_error = self.state.account_error is not None
            err_msg = self.state.account_error
                
        # 組合保證金摘要與持倉表格
        if has_error:
            error_msg_panel = Panel(
                Text(f"⚠️ {err_msg}", style="bold yellow"),
                border_style="yellow",
                box=box.ROUNDED
            )
            content = Group(
                error_msg_panel,
                Text("─" * 80, style="dim white"),
                info_text,
                Text("─" * 80, style="dim white"),
                pos_table
            )
        else:
            content = Group(
                info_text,
                Text("─" * 80, style="dim white"),
                pos_table
            )
        
        return Panel(content, title="💼 期貨帳戶保證金與持倉部位", border_style="yellow")

    def query_account_once(self) -> Panel:
        """
        同步查詢一次帳戶保證金與部位資料。
        用於單次查詢模式，完成後返回渲染面板。
        """
        if not Config.has_credentials():
            self.logger.warning("未偵測到 SHIOAJI_API_KEY 或 SHIOAJI_SECRET_KEY 變數，將採用 Mock 模擬帳戶數據。")
            self.state.mode = "Mock 模擬模式"
            # 產生一組 Mock 數據
            with self.state.lock:
                self.state.equity = 1500000.00
                self.state.available_margin = 1290000.00
                self.state.maintenance_margin = 150000.00
                self.state.risk_indicator = 86.50
                self.state.positions = [{
                    'code': Config.FUTURES_CODE,
                    'direction': "多",
                    'quantity': 1,
                    'price': 21450.00,
                    'pnl': 3000.0  # 模擬持倉損益
                }]
                self.state.futures_pnl = 3000.0
                self.state.futures_price = 21465.00
                self.state.account_error = None
            return self._generate_account_panel()

        try:
            self.logger.info("正在連線至永豐期貨 API 進行單次查詢...")
            self.api = sj.Shioaji(simulation=Config.SIMULATION)
            
            self.logger.info("進行 API 金鑰驗證...")
            self.api.login(api_key=Config.API_KEY, secret_key=Config.SECRET_KEY)
            self.logger.info("永豐 API 驗證登入成功！")
            
            # 查詢帳戶
            account = getattr(self.api, 'futopt_account', None)
            if not account:
                raise ValueError("查無有效的期貨選擇權交易帳戶 (futopt_account)")
                
            self.logger.info("發送 API 帳戶保證金查詢...")
            margin = self.api.margin(account)
            
            self.logger.info("發送 API 持倉部位查詢...")
            positions_raw = self.api.list_positions(account)
            
            # 我們也嘗試獲取當前商品的快照，以便顯示當前估價
            snapshot_prices = {}
            if positions_raw:
                try:
                    self.logger.info("查詢持倉商品最新市場快照...")
                    contracts = []
                    for pos in positions_raw:
                        code = getattr(pos, 'code', '')
                        if code:
                            try:
                                contract = getattr(self.api.Contracts.Futures.TXF, code, None)
                                if contract:
                                    contracts.append(contract)
                            except Exception:
                                pass
                    if contracts:
                        snaps = self.api.snapshots(contracts)
                        if snaps:
                            for snap in snaps:
                                c = getattr(snap, 'code', '')
                                snapshot_prices[c] = float(getattr(snap, 'close', 0.0) or 0.0)
                except Exception as snap_err:
                    self.logger.warning(f"單次查詢時獲取持倉商品快照價格失敗: {snap_err}")

            with self.state.lock:
                self.state.mode = "SinoPac API 實時"
                self.state.account_error = None
                
                if margin:
                    self.state.yesterday_balance = float(getattr(margin, 'yesterday_balance', 0.0) or 0.0)
                    self.state.today_balance = float(getattr(margin, 'today_balance', 0.0) or 0.0)
                    self.state.equity = float(getattr(margin, 'equity', 0.0) or 0.0)
                    self.state.available_margin = float(getattr(margin, 'available_margin', 0.0) or 0.0)
                    self.state.maintenance_margin = float(getattr(margin, 'maintenance_margin', 0.0) or 0.0)
                    self.state.risk_indicator = float(getattr(margin, 'risk_indicator', 0.0) or 0.0)
                    self.state.futures_pnl = float(getattr(margin, 'future_open_position', 0.0) or 0.0)
                    
                new_positions = []
                if positions_raw:
                    for pos in positions_raw:
                        code = getattr(pos, 'code', '')
                        dir_val = getattr(pos, 'direction', 'Buy')
                        dir_str = "多" if "Buy" in str(dir_val) else "空"
                        qty = int(getattr(pos, 'quantity', 0))
                        cost = float(getattr(pos, 'price', 0.0))
                        pnl = float(getattr(pos, 'pnl', 0.0))
                        
                        if code in snapshot_prices and snapshot_prices[code] > 0:
                            if code == Config.FUTURES_CODE:
                                self.state.futures_price = snapshot_prices[code]
                        
                        new_positions.append({
                            'code': code,
                            'direction': dir_str,
                            'quantity': qty,
                            'price': cost,
                            'pnl': pnl
                        })
                self.state.positions = new_positions
                
        except Exception as e:
            err_msg = str(e)
            self.logger.error(f"單次查詢帳戶資訊時發生錯誤: {err_msg}")
            with self.state.lock:
                if "406" in err_msg or "Account Not Acceptable" in err_msg:
                    self.state.account_error = "查詢失敗 (錯誤碼 406): 請登入永豐期貨官網簽署「API電子交易風險預告書暨使用同意書」"
                else:
                    self.state.account_error = f"查詢失敗: {err_msg}"
        finally:
            if self.api:
                try:
                    self.api.logout()
                except Exception:
                    pass
                    
        return self._generate_account_panel()

    def generate_minimal_renderable(self) -> Text:
        """
        生成極簡模式的渲染內容（僅包含時間與期貨價格，不含表格和邊框）
        """
        with self.state.lock:
            price_val = self.state.futures_price
            change_val = self.state.futures_change
            pct_val = self.state.futures_change_pct
            
            time_val = self.state.futures_time
            if time_val == "-":
                tw_tz = pytz.timezone('Asia/Taipei')
                time_val = datetime.now(tw_tz).strftime("%H:%M:%S")
            
            if change_val > 0:
                price_str = f"{price_val:.0f}"
                change_str = f"+{change_val:.0f}"
                pct_str = f"+{pct_val:.2f}%"
                color = "bold red"
            elif change_val < 0:
                price_str = f"{price_val:.0f}"
                change_str = f"{change_val:.0f}"
                pct_str = f"{pct_val:.2f}%"
                color = "bold green"
            else:
                price_str = f"{price_val:.0f}"
                change_str = "0"
                pct_str = "0.00%"
                color = "white"
                
            text = Text()
            text.append(f"[{time_val}] ", style="cyan")
            text.append(f"台指期貨近月 ({Config.FUTURES_CODE}): ", style="bold white")
            text.append(price_str, style=color)
            text.append(f" ({change_str} / {pct_str})", style=color)
            
            return text

    def stop(self):
        """
        停止監控服務。
        """
        self.logger.info("正在關閉行情監控系統...")
        self.running = False
        
        if self._mock_thread and self._mock_thread.is_alive():
            self._mock_thread.join(timeout=2.0)
            self.logger.info("Mock 模擬行情產生器已安全關閉。")

        if self.api:
            try:
                self.logger.info("正在登出永豐 API 連線...")
                self.api.logout()
                self.logger.info("永豐 API 連線已安全登出。")
            except Exception as e:
                self.logger.error(f"登出永豐 API 時發生錯誤: {e}")
                
        self.logger.info("系統關閉作業完成。")
