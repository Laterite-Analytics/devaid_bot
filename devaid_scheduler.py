import logging
import time

import schedule

import devaid

logging.basicConfig(level=logging.INFO)


def job():
    logging.info("Running scheduled job...")
    new_tender_ids = devaid.fetch_new_tenders()
    devaid.fetch_multiple_tenders_details(new_tender_ids[:5])


# schedule.every(5).minutes.do(job)  # for testing
for day in ["monday", "tuesday", "wednesday", "thursday", "friday"]:
    getattr(schedule.every(), day).at("07:00").do(job)

while True:
    schedule.run_pending()
    logging.info("Schedule launched")
    time.sleep(120)
