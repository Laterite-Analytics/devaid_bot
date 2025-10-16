import time

import schedule

import devaid


def job():
    print("Running scheduled job...")
    new_tender_ids = devaid.fetch_new_tenders()
    devaid.fetch_multiple_tenders_details(new_tender_ids[:5])


schedule.every(5).minutes.do(job)  # for testing
# schedule.every().tuesday.at("09:00").do(job)  # for production

while True:
    schedule.run_pending()
    time.sleep(120)
