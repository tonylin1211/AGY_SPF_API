import os
from dotenv import load_dotenv

# 載入當前目錄下的 .env 檔案中的環境變數
load_dotenv()

class Config:
    """
    設定類別，負責管理永豐 API 連線金鑰、運行模式等參數。
    """
    # 永豐 API 金鑰 (API Key)
    API_KEY: str = os.getenv("SHIOAJI_API_KEY", "").strip()
    
    # 永豐 API 密鑰 (Secret Key)
    SECRET_KEY: str = os.getenv("SHIOAJI_SECRET_KEY", "").strip()
    
    # 是否開啟模擬交易環境，預設為 True
    SIMULATION: bool = os.getenv("SHIOAJI_SIMULATION", "True").strip().lower() in ("true", "1", "yes", "on")

    # 永豐憑證路徑 (CA Certificate Path, .pfx 檔案)
    CA_PATH: str = os.getenv("SHIOAJI_CA_PATH", "").strip()
    
    # 永豐憑證密碼 (CA Password)
    CA_PASSWORD: str = os.getenv("SHIOAJI_CA_PASSWORD", "").strip()
    
    # 身份證字號/統一編號 (Person ID, 若不提供則程式自動從期貨帳戶中抓取)
    PERSON_ID: str = os.getenv("SHIOAJI_PERSON_ID", "").strip()

    @classmethod
    def has_ca_credentials(cls) -> bool:
        """
        檢查是否已填寫 CA 憑證路徑與密碼。
        """
        return bool(cls.CA_PATH and cls.CA_PASSWORD)

    # 訂閱的期貨與大盤商品代碼定義
    # 台股期貨近月合約 (TXFR1)，會自動於換月時更新
    FUTURES_CODE: str = "TXFR1"
    
    # 加權大盤指數代碼 (TSE001)
    INDEX_CODE: str = "TSE001"

    @classmethod
    def has_credentials(cls) -> bool:
        """
        檢查是否已填寫必要的 API 金鑰與密鑰。
        
        回傳:
            bool: 若金鑰與密鑰皆不為空，回傳 True；否則回傳 False。
        """
        return bool(cls.API_KEY and cls.SECRET_KEY)

    @classmethod
    def get_summary(cls) -> str:
        """
        取得當前設定摘要，用於日誌記錄。
        金鑰會進行遮罩處理以確保安全性。
        """
        masked_key = f"{cls.API_KEY[:4]}...{cls.API_KEY[-4:]}" if len(cls.API_KEY) > 8 else "未設定"
        return (
            f"API Key: {masked_key}, "
            f"Simulation Mode: {cls.SIMULATION}, "
            f"Futures Target: {cls.FUTURES_CODE}, "
            f"Index Target: {cls.INDEX_CODE}"
        )
