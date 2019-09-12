import signal
import sys
from os import path
import json
import pkg_resources
import asyncio
import logging
from datetime import datetime

from . import db, models, settings
from .exchange_listener import ExchangeListener
from .orchestrator import Orchestrator
from . import market_crawler
from .ob_snapshot_generator import OBSnapshotGenerator



def init_db(args):
    db.Base.metadata.create_all(db.engine)
    fixtures = [("coins.json", models.Coin, "symbol"),
                ("exchanges.json", models.Exchange, "name")]
    for filename, Model, column in fixtures:
        filepath = path.join("fixtures", filename)
        entities = pkg_resources.resource_string(settings.PACKAGE, filepath)
        entities = json.loads(entities)

        for entity in entities:
            if Model.query.filter_by(**{column: entity[column]}).first():
                continue
            model = Model(**entity)
            db.session.add(model)

        db.session.commit()

def run(args):
    if args["exchange"]:
        exchange = args["exchange"]
    else:
        exchange = ExchangeListener.registered()
    orchestrator = Orchestrator(exchange)
    def handler(_signum, _frame):
        orchestrator.stop()
    signal.signal(signal.SIGINT, handler)
    try:
        asyncio.get_event_loop().run_until_complete(orchestrator.start())
    except KeyboardInterrupt:
        orchestrator.stop()

def markets(args):
    try:
        asyncio.get_event_loop().run_until_complete(_markets(args))
    except KeyboardInterrupt:
        logging.info("stop 'markets'")    

async def _markets(args):
    if args["exchange"]:
        exchange = args["exchange"]
    else:
        exchange = ExchangeListener.registered()
    orchestrator = Orchestrator(exchange)
    def handler(_signum, _frame):
        orchestrator.stop()
    signal.signal(signal.SIGINT, handler)
    try:
        await orchestrator.get_markets()
    except KeyboardInterrupt:
        orchestrator.stop()

def init_data(args):
    try:
        asyncio.get_event_loop().run_until_complete(_init_data(args))
    except KeyboardInterrupt:
        logging.info("stop init-data")

async def _init_data(args):
    logging.info("fetching markets from exchanges")
    await _markets(args)
    logging.info("fetching latest price in USD for each coin")
    await _fetch_prices(args)
    logging.info("normalising traded volume in USD for all exchanges")    
    norm_volume(args)
    
def fetch_prices(args):
    try:
        asyncio.get_event_loop().run_until_complete(_fetch_prices(args))
    except KeyboardInterrupt:
        logging.info("stop 'fetch-prices'")    

async def _fetch_prices(args):
    await start_crawler()

async def start_crawler():
    n = 0
    update_actions = []
    crawler = market_crawler.MarketCrawler()
    coins = models.Coin.query.all()
    for coin in coins:
        coin.price_usd = await crawler.get_price(coin.symbol)
        coin.last_price_updated = datetime.now()
        if coin.name is None:
            coin.name = crawler.get_coin_name(coin.symbol)
        logging.debug("PRICE UPDATE - %s: %s USD", coin.symbol, coin.price_usd)
        db.session.add(coin)
        n += 1
    logging.info("UPDATE - %s coin prices have been updated in antalla db", n)
    db.session.commit()

def norm_volume(args):
    if args["exchange"]:
        exchanges = args["exchange"]
    else:
        exchanges = ExchangeListener.registered()
    for e in exchanges:
        exchange = models.Exchange.query.filter_by(name=e).all()
        if exchange != None:
            id = exchange[0].id
            set_usd_vol(id)
            logging.info("usd volume computed for exchange: '%s'", exchange[0].name)
        else:
            logging.warning("exchange '%s' not found in db - check '--exchange' flag is set with correct argument", e)

def set_usd_vol(exchange_id):
    exchange_markets = models.ExchangeMarket.query.filter_by(exchange_id=exchange_id).all()
    for exm in exchange_markets:
        coin_price = get_usd_price(exm.quoted_volume_id)
        if exm.quoted_volume is None:
            logging.warning("no quoted volume for pair '{}-{}' on exchange id '{}'".format(exm.first_coin_id, exm.second_coin_id, exchange_id)) 
        else: 
            exm.volume_usd = coin_price * exm.quoted_volume
            exm.vol_usd_timestamp = datetime.now()
            logging.debug("UPDATE 'volume_usd' - exchange id: {} - usd volume: {} - market: '{}-{}' - timestamp: {}".format(
                exm.exchange_id, exm.volume_usd, exm.first_coin_id, exm.second_coin_id, exm.vol_usd_timestamp
            ))
            db.session.add(exm)
    db.session.commit()

def get_usd_price(symbol):
    coin = models.Coin.query.get(symbol.upper())
    if coin is None:
        logging.debug("no USD price for symbol '%s' in db", symbol)
        return 0
    else:
        return coin.price_usd

def snapshot(args):
    if args["exchange"]:
        exchanges = args["exchange"]
    else:
        exchanges = ExchangeListener.registered()
    stop_time = datetime.now()
    obs_generator = OBSnapshotGenerator(exchanges, stop_time) 
    try:
        obs_generator.run()
    except KeyboardInterrupt:
        logging.warning("KeybaordInterrupt - 'obs_generator.run()'")
        sys.exit()