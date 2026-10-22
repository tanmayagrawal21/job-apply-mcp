"""Fictional API payloads shaped like the real platform responses (no real companies or people)."""
import html

GH_DESC = ("<p>Build services in Python. 1-3 years of professional experience required. "
           "Pay range: $120,000 - $150,000 per year. We are unable to provide visa sponsorship.</p>")
GH_LIST = {"jobs": [
    {"id": 101, "title": "Software Engineer, New Grad", "updated_at": "2026-10-20T10:00:00-04:00",
     "absolute_url": "https://job-boards.greenhouse.io/acmeco/jobs/101", "location": {"name": "Remote, US"},
     "company_name": "Acme Co", "content": html.escape(GH_DESC)},
    {"id": 102, "title": "Staff Designer", "updated_at": "2026-01-02T10:00:00-04:00",
     "absolute_url": "https://job-boards.greenhouse.io/acmeco/jobs/102", "location": {"name": "Berlin"},
     "company_name": "Acme Co", "content": html.escape("<p>Design things.</p>")},
], "meta": {"total": 2}}
GH_JOB = {**GH_LIST["jobs"][0], "questions": [
    {"label": "First Name", "required": True, "fields": [{"name": "first_name", "type": "input_text", "values": []}]},
    {"label": "Resume/CV", "required": True, "fields": [{"name": "resume", "type": "input_file", "values": []},
                                                       {"name": "resume_text", "type": "textarea", "values": []}]},
    {"label": "Will you now or in the future require sponsorship?", "required": True,
     "fields": [{"name": "question_1", "type": "multi_value_single_select",
                 "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]}]},
    {"label": "Preferred tools", "required": False,
     "fields": [{"name": "question_2", "type": "multi_value_multi_select",
                 "values": [{"label": "Python", "value": 11}, {"label": "Go", "value": 12}]}]},
], "location_questions": []}

LEVER_ID = "0a1b2c3d-1111-2222-3333-444455556666"
LEVER_LIST = [{
    "id": LEVER_ID, "text": "Backend Engineer", "categories": {"location": "New York, NY", "team": "Platform"},
    "createdAt": 1760000000000, "hostedUrl": f"https://jobs.lever.co/democo/{LEVER_ID}",
    "applyUrl": f"https://jobs.lever.co/democo/{LEVER_ID}/apply",
    "descriptionPlain": "Work on APIs. Minimum 5 years of experience. Must be a U.S. citizen.",
    "lists": [{"text": "Requirements", "content": "<li>Go</li><li>SQL</li>"}], "additionalPlain": "",
    "salaryRange": {"min": 140000, "max": 170000, "currency": "USD", "interval": "per-year-salary"}}]

ASHBY_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ASHBY_BOARD = {"jobs": [
    {"id": ASHBY_ID, "title": "ML Engineer", "location": "San Francisco", "publishedAt": "2026-10-19T00:00:00.000+00:00",
     "jobUrl": f"https://jobs.ashbyhq.com/exampleai/{ASHBY_ID}", "isListed": True,
     "descriptionPlain": "Train models. Graduating in Fall 2027 preferred.",
     "compensation": {"compensationTierSummary": "$150K - $200K"}},
    {"id": "hidden", "title": "Hidden", "isListed": False, "jobUrl": "https://jobs.ashbyhq.com/exampleai/hidden"}]}
ASHBY_FORM = {"data": {"jobPosting": {"applicationForm": {"sections": [{"fieldEntries": [
    {"isRequired": True, "field": {"path": "_systemfield_name", "title": "Name", "type": "String"}},
    {"isRequired": True, "field": {"path": "_systemfield_resume", "title": "Resume", "type": "File"}},
    {"isRequired": False, "field": {"path": "q1", "title": "Work authorization", "type": "ValueSelect",
                                    "selectableValues": [{"label": "Yes", "value": "y"}, {"label": "No", "value": "n"}]}},
]}]}}}}

SR_LIST = {"offset": 0, "limit": 100, "totalFound": 1, "content": [
    {"id": "7430001", "name": "Data Analyst", "releasedDate": "2026-10-18T12:00:00.000Z",
     "location": {"city": "Austin", "region": "TX", "country": "us", "fullLocation": "Austin, TX, United States"},
     "company": {"name": "Sample Retail"}}]}
SR_JOB = {**SR_LIST["content"][0], "applyUrl": "https://jobs.smartrecruiters.com/samplecorp/7430001?oga=true",
          "postingUrl": "https://jobs.smartrecruiters.com/samplecorp/7430001-data-analyst",
          "jobAd": {"sections": {"jobDescription": {"title": "Job", "text": "<p>Analyze data. 2+ years experience.</p>"},
                                 "qualifications": {"title": "Q", "text": "<p>SQL. Hourly $30 - $40 per hour.</p>"}}}}

WD_SEARCH = {"total": 2, "jobPostings": [
    {"title": "Associate Engineer", "externalPath": "/job/Phoenix-AZ/Associate-Engineer_JR100",
     "locationsText": "Phoenix, AZ", "postedOn": "Posted 3 Days Ago", "bulletFields": ["JR100"]},
    {"title": "Technician", "externalPath": "/job/Austin-TX/Technician_JR101",
     "locationsText": "Austin, TX", "postedOn": "Posted 30+ Days Ago", "bulletFields": ["JR101"]}]}
WD_JOB = {"jobPostingInfo": {"title": "Associate Engineer", "location": "Phoenix, AZ", "postedOn": "Posted 3 Days Ago",
                             "startDate": "2026-10-19", "canApply": True,
                             "externalUrl": "https://demo.wd5.myworkdayjobs.com/en-US/External/job/Phoenix-AZ/Associate-Engineer_JR100",
                             "jobDescription": "<p>Build things. Security clearance required.</p>"},
          "hiringOrganization": {"name": "Demo Industries"}}
