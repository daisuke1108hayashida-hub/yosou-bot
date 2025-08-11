# db.py
import os
from datetime import datetime
from typing import Optional, Dict, Any, List

from sqlalchemy import (
    create_engine, Column, Integer, String, Date, DateTime, JSON, Text, Boolean, UniqueConstraint
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL")  # Render の PostgreSQL をここに
if not DATABASE_URL:
    # ローカル検証用に SQLite を使えるようにしておく
    DATABASE_URL = "sqlite:///./local.db"

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

class RacePrediction(Base):
    __tablename__ = "race_predictions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    race_key: Mapped[str] = mapped_column(String(64))      # 例: "HMN-20250811-12"
    venue:    Mapped[str] = mapped_column(String(32))
    date:     Mapped[str] = mapped_column(String(8))       # YYYYMMDD
    race_no:  Mapped[int] = mapped_column(Integer)

    features: Mapped[Dict[str, Any]] = mapped_column(JSON) # 直前情報やコース成績など
    main:     Mapped[List] = mapped_column(JSON)           # 本線 買い目の配列
    osae:     Mapped[List] = mapped_column(JSON)           # 抑え
    narai:    Mapped[List] = mapped_column(JSON)           # 狙い
    explanation: Mapped[str] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    settled: Mapped[bool] = mapped_column(Boolean, default=False)
    hit:     Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)  # 当たり/ハズレ
    payout:  Mapped[Optional[int]]  = mapped_column(Integer, nullable=True)  # 3連単配当（任意）

    __table_args__ = (
        UniqueConstraint("race_key", name="uq_race_key_once"),
    )

class WeightProfile(Base):
    """
    シンプルな場ごとの重み。 venue="GLOBAL" を全体デフォルトとして使う。
    """
    __tablename__ = "weight_profiles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    venue: Mapped[str] = mapped_column(String(32), unique=True)
    weights: Mapped[Dict[str, float]] = mapped_column(JSON)   # {"tenji":..,"syuukai":..,"mawari":..,"chokusen":..,"st":..}
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

def init_db():
    Base.metadata.create_all(bind=engine)

def get_session():
    return SessionLocal()

# ====== 便利関数 ======
DEFAULT_WEIGHTS = {"tenji": 0.30, "syuukai": 0.20, "mawari": 0.25, "chokusen": 0.15, "st": 0.10}

def load_weights(venue: str) -> Dict[str, float]:
    with get_session() as s:
        wp = s.query(WeightProfile).filter_by(venue=venue).first()
        if not wp:
            # なければ GLOBAL → さらにデフォルト
            wp = s.query(WeightProfile).filter_by(venue="GLOBAL").first()
            if not wp:
                wp = WeightProfile(venue="GLOBAL", weights=DEFAULT_WEIGHTS)
                s.add(wp); s.commit()
        return dict(wp.weights)

def save_prediction(race_key: str, venue: str, date: str, race_no: int,
                    features: Dict[str, Any], main, osae, narai, explanation: str):
    with get_session() as s:
        rec = RacePrediction(
            race_key=race_key, venue=venue, date=date, race_no=race_no,
            features=features, main=main, osae=osae, narai=narai, explanation=explanation
        )
        s.add(rec)
        try:
            s.commit()
        except Exception:
            s.rollback()  # 同一レースで2回保存しようとしたときの保険

def settle_result(race_key: str, trifecta: str, payout: Optional[int] = None):
    """
    結果を反映して簡単な学習（重み微調整）を行う。
    trifecta 例: "1-2-3"
    """
    with get_session() as s:
        pred = s.query(RacePrediction).filter_by(race_key=race_key).first()
        if not pred or pred.settled:
            return False

        all_picks = set(tuple(p.split("-")) for lst in (pred.main or [])+(pred.osae or [])+(pred.narai or []) for p in (lst if isinstance(lst, list) else [lst]))
        hit = tuple(trifecta.split("-")) in all_picks
        pred.settled = True
        pred.hit = bool(hit)
        if payout is not None:
            pred.payout = payout
        s.add(pred)

        # 超シンプルな学習：当たりなら重みをほんの少し拡大、外れなら縮小
        venue = pred.venue or "GLOBAL"
        wp = s.query(WeightProfile).filter_by(venue=venue).first()
        if not wp:
            wp = s.query(WeightProfile).filter_by(venue="GLOBAL").first()
        if wp:
            w = dict(wp.weights)
            eta = 0.02  # 学習率
            if hit:
                for k in w: w[k] *= (1 + eta)
            else:
                for k in w: w[k] *= (1 - eta)
            # 正規化
            ssum = sum(w.values()) or 1.0
            for k in w: w[k] = round(w[k] / ssum, 4)
            wp.weights = w
            s.add(wp)

        s.commit()
        return True
