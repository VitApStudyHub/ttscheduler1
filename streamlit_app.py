import streamlit as st
import pandas as pd
import os
import pickle
import re
import json
from datetime import datetime, timedelta, date

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

##########################
# 1) Constants (shared)
##########################
SCOPES = ["https://www.googleapis.com/auth/calendar"]
REDIRECT_URI = "https://timetable.vitaphub.in"  # or your domain
TIMEZONE = "Asia/Kolkata"

# A global weekday_map so day_code -> integer (Mon=0,...,Sun=6)
weekday_map = {
    "MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6
}

def load_mappings(file_path):
    """Helper to read an external JSON file containing 'theory_mapping' and 'lab_mapping'."""
    with open(file_path, "r") as f:
        data = json.load(f)
    return data["theory_mapping"], data["lab_mapping"]

##########################
# 2) Google Auth
##########################
def get_google_calendar_service():
    """Check for ?code=... from Google sign-in or existing token; return a Calendar service if available."""
    if "google_token" in st.session_state:
        creds = pickle.loads(st.session_state["google_token"])
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                st.session_state["google_token"] = pickle.dumps(creds)
            except Exception as e:
                st.error(f"Could not refresh token: {e}")
                del st.session_state["google_token"]
                creds = None
        if creds and creds.valid:
            return build("calendar", "v3", credentials=creds)

    if not os.path.exists("credentials.json"):
        st.error("Missing credentials.json!")
        return None

    flow = InstalledAppFlow.from_client_secrets_file(
        "credentials.json",
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI
    )

    query_params = st.experimental_get_query_params()
    code = query_params.get("code", [None])[0]
    if code:
        try:
            flow.fetch_token(code=code)
            creds = flow.credentials
            st.session_state["google_token"] = pickle.dumps(creds)
            st.experimental_set_query_params()  # Clear query params
            st.success("Google authentication successful! You may close the sign-in tab.")
            return build("calendar", "v3", credentials=creds)
        except Exception as e:
            st.error(f"Error fetching token: {e}")
            return None

    return None

def open_auth_url_in_new_tab():
    """Generate the OAuth URL, auto-open in new tab, also return fallback link."""
    if not os.path.exists("credentials.json"):
        st.error("Missing credentials.json!")
        return None

    flow = InstalledAppFlow.from_client_secrets_file(
        "credentials.json",
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI
    )
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")

    auto_open_script = f"""
    <script>
        window.open("{auth_url}", "_blank");
    </script>
    """
    st.markdown(auto_open_script, unsafe_allow_html=True)

    link_html = f'<a href="{auth_url}" target="_blank">click here to sign in manually</a>'
    return link_html

##########################
# 3) Timetable Extraction
##########################
def extract_course_details(text):
    """
    Regex-based parser for lines that match the typical VTOP format.
    """
    pattern = re.compile(
        r"(\w+\d+ - [\w\s-]+(?:\s+(?:I|II|III|IV|V|VI|VII|VIII|IX|X))?)\s*\(([\w\s]+)\)\s*"
        r"[\d\s.]+\s*-\s*Regular\s*([\w\d]+)\s*([\w\d\+\-]+)\s*-\s*([\w\d-]+)\s*([\w\s.]+)\s*-\s*([\w]+)",
        re.IGNORECASE
    )
    courses = []
    for match in pattern.finditer(text):
        try:
            course_name = match.group(1).strip()
            course_type = match.group(2).strip()
            if course_type.lower() in ["embedded theory", "embedded lab", "theory only"]:
                full_course = course_name
            else:
                full_course = f"{course_name} ({course_type})"

            slot = match.group(4).strip()
            venue = match.group(5).strip()
            facstr = match.group(6).strip()
            facdep = match.group(7).strip()
            faculty = f"{facstr} - {facdep}"

            courses.append({
                "Course": full_course,
                "Slot": slot,
                "Venue": venue,
                "Faculty Details": faculty,
            })
        except Exception as e:
            st.error(f"Error processing course: {str(e)}")
            continue
    return courses

##########################
# 4) Calendar Creation
##########################
def get_or_create_calendar(service, calendar_name, timezone=TIMEZONE):
    if not service:
        return None
    cals = service.calendarList().list().execute()
    for c in cals.get("items", []):
        if c.get("summary") == calendar_name:
            return c.get("id")
    body = {"summary": calendar_name, "timeZone": timezone}
    new_cal = service.calendars().insert(body=body).execute()
    return new_cal.get("id")

def get_first_date_on_or_after(start_date, target_weekday):
    days_ahead = target_weekday - start_date.weekday()
    if days_ahead < 0:
        days_ahead += 7
    return start_date + timedelta(days_ahead)

##########################
# 5) Create Events with Skip Logic
##########################
def create_calendar_events(service, df, calendar_id,
                           from_date, until_str,
                           skip_ranges,
                           theory_mapping, lab_mapping,
                           notifications=[]):
    """
    Creates weekly recurring events, skipping entire date ranges in skip_ranges.
    """
    if not service or not calendar_id:
        return False

    overrides = [{"method": "popup", "minutes": m} for m in notifications]
    reminders = {"useDefault": False, "overrides": overrides}

    total_rows = len(df)
    prog_bar = st.progress(0)
    success = True

    for idx, row in df.iterrows():
        course = row.get("Course", "").strip()
        slot_field = row.get("Slot", "").strip()
        venue = row.get("Venue", "").strip()
        faculty = row.get("Faculty Details", "").strip()

        # Skip certain lines
        if "EMBEDDED PROJECT" in course.upper():
            continue
        if "NIL-ONL" in venue.upper():
            continue

        summary = f"{course} [{slot_field}]"

        # Lab vs. theory detection
        slot_tokens = [tok.strip().upper() for tok in slot_field.split("+") if tok.strip()]
        is_lab = False
        lab_key = None

        if slot_field.upper() in lab_mapping:
            is_lab = True
            lab_key = slot_field.upper()
        else:
            for tok in slot_tokens:
                if tok in lab_mapping or tok.startswith("L"):
                    is_lab = True
                    lab_key = tok
                    break

        day_time_pairs = []
        try:
            if is_lab:
                mapping = lab_mapping.get(lab_key)
                if not mapping:
                    st.warning(f"Lab slot '{lab_key}' not found. Skipping row {idx}.")
                    continue
                day_time_pairs = mapping
            else:
                # Theory
                mapping = []
                for token in slot_tokens:
                    if token in theory_mapping:
                        mapping.extend(theory_mapping[token])
                    else:
                        st.warning(f"Theory slot '{token}' not found. Skipping row {idx}.")
                day_time_pairs = mapping

            if not day_time_pairs:
                continue

            # Build event body for each day/time pair
            for (day_code, start_str, end_str) in day_time_pairs:
                try:
                    sh, sm = map(int, start_str.split(":"))
                    eh, em = map(int, end_str.split(":"))
                except:
                    st.warning(f"Invalid time '{start_str}' or '{end_str}' in row {idx}. Skipping.")
                    continue

                wd = weekday_map[day_code]
                first_occ_date = get_first_date_on_or_after(from_date, wd)

                dtstart = datetime.combine(first_occ_date, datetime.min.time()).replace(hour=sh, minute=sm)
                dtend   = datetime.combine(first_occ_date, datetime.min.time()).replace(hour=eh, minute=em)

                rrule = f"RRULE:FREQ=WEEKLY;BYDAY={day_code};UNTIL={until_str}"

                # EXDATE lines for skip_ranges
                skip_exdates = []
                for (skip_start, skip_end) in skip_ranges:
                    day_count = (skip_end - skip_start).days + 1
                    for i in range(day_count):
                        skip_day = skip_start + timedelta(days=i)
                        ex_str = skip_day.strftime("%Y%m%dT") + f"{sh:02d}{sm:02d}00"
                        skip_exdates.append(f"EXDATE;TZID={TIMEZONE}:{ex_str}")

                recurrence_lines = [rrule] + skip_exdates

                event_body = {
                    "summary": summary,
                    "location": venue,
                    "description": faculty,
                    "start": {"dateTime": dtstart.isoformat(), "timeZone": TIMEZONE},
                    "end":   {"dateTime": dtend.isoformat(),   "timeZone": TIMEZONE},
                    "recurrence": recurrence_lines,
                    "reminders": reminders,
                }
                service.events().insert(calendarId=calendar_id, body=event_body).execute()

        except Exception as e:
            st.error(f"Error creating event for {course}: {str(e)}")
            success = False

        progress_val = int(((idx + 1) / total_rows) * 100)
        prog_bar.progress(min(progress_val, 100))

    prog_bar.progress(100)
    return success

##########################
# 6) Multi-Step UI
##########################
def main():
    st.title("Get Classes Schedules on Google Calendar")

    if "step" not in st.session_state:
        st.session_state["step"] = 1

    # We'll store batch choice in session_state
    if "batch" not in st.session_state:
        st.session_state["batch"] = None

    if st.session_state["step"] == 1:
        st.header("Step 1: Authorize, then Select Batch & Upload/Paste Timetable")
        st.markdown(
            "How to Use Text Guide, [www.vitaphub.in/guide](https://www.vitaphub.in/guide)"
        )

        # 1) Sign in with Google (above batch selection)
        service = get_google_calendar_service()
        if service:
            st.success("You are already authenticated with Google Calendar!")
        else:
            st.warning("Not authenticated. Please sign in below.")
            if st.button("Sign in with Google"):
                link_html = open_auth_url_in_new_tab()
                if link_html:
                    st.markdown(f"If new tab didn't open, {link_html}", unsafe_allow_html=True)

        # 2) Batch selection
        st.subheader("Which batch are you in?")
        batch_option = st.radio(
            "Make Sure Correct Batch is Selected After Refresh:",
            ["Only 2024 Batch", "All Other Batches"],
            index=1
        )
        st.session_state["batch"] = batch_option

        # Based on batch selection, define constraints
        if batch_option == "Only 2024 Batch":
            # Use mapping1.json
            mapping_file = "mapping1.json"
            st.session_state["SEMESTER_START"] = date(2025, 1, 27)
            st.session_state["SEMESTER_END_STR"] = "20250516T235959Z"
            st.session_state["SKIP_RANGES"] = [
                (datetime(2025, 2, 24), datetime(2025, 3, 3)),
                (datetime(2025, 4, 7), datetime(2025, 4, 15))
            ]
            try:
                t_map, l_map = load_mappings(mapping_file)
                st.session_state["theory_map"] = t_map
                st.session_state["lab_map"] = l_map
            except Exception as e:
                st.error(f"Could not load {mapping_file}: {e}")
                st.stop()
        else:
            # All Other Batches => mappings.json
            mapping_file = "mappings.json"
            st.session_state["SEMESTER_START"] = date(2024, 12, 1)
            st.session_state["SEMESTER_END_STR"] = "20250425T235959Z"
            st.session_state["SKIP_RANGES"] = [
                (datetime(2025, 3, 22), datetime(2025, 3, 29))
            ]
            try:
                t_map, l_map = load_mappings(mapping_file)
                st.session_state["theory_map"] = t_map
                st.session_state["lab_map"] = l_map
            except Exception as e:
                st.error(f"Could not load {mapping_file}: {e}")
                st.stop()

        st.write("---")
        st.subheader("Timetable Input")

        method = st.radio("Input Method", ["Upload CSV", "Paste Timetable Text (Recommended)"])
        if method == "Upload CSV":
            csv_file = st.file_uploader("Upload CSV", type=["csv"])
            if csv_file:
                df = pd.read_csv(csv_file, skipinitialspace=True)
                df.columns = [c.strip() for c in df.columns]
                st.session_state["df"] = df
                st.write("### CSV Preview")
                st.dataframe(df)
        else:
            text = st.text_area("Paste your timetable text:", height=300)
            st.markdown(
                "How to Get Timetable Text Guide, [www.vitaphub.in/guide](https://www.vitaphub.in/guide)"
            )

            if text:
                try:
                    courses = extract_course_details(text)
                    if courses:
                        df = pd.DataFrame(courses)
                        st.write("### Parsed Data Preview (Editable)")
                        st.info("Optional: Download CSV below to edit offline, then re-upload if needed.")
                        st.warning("Project Courses will not be added, so no need to remove them from the table.")

                        edited_df = st.data_editor(df, num_rows="dynamic", key="editor")
                        st.session_state["df"] = edited_df
                    else:
                        st.warning("No courses extracted. Check your input format.")
                except Exception as e:
                    st.error(f"Error parsing: {e}")

        # If we have a DF, show the Download button (optional), then Next
        if "df" in st.session_state:
            final_df = st.session_state["df"]
            csv_data = final_df.to_csv(index=False)

            # Download CSV button (optional)
            st.download_button(
                label="Download CSV",
                data=csv_data,
                file_name="parsed_timetable.csv",
                mime="text/csv"
            )

            # Next -> Step 2 button
            if st.button("Next -> Step 2"):
                if "google_token" not in st.session_state:
                    st.error("Please sign in with Google first!")
                    st.stop()
                st.session_state["step"] = 2
                st.stop()
        else:
            # If no DF yet, next step won't work
            if st.button("Next -> Step 2"):
                if "google_token" not in st.session_state:
                    st.error("Please sign in with Google first!")
                    st.stop()
                if "df" not in st.session_state:
                    st.error("Please provide timetable data first!")
                    st.stop()
                st.session_state["step"] = 2
                st.stop()

    elif st.session_state["step"] == 2:
        st.header("Step 2: Notifications (minutes before Class)")
        st.markdown(
            "Notification Settings Text Guide, [www.vitaphub.in/guide](https://www.vitaphub.in/guide)"
        )

        with st.form("notif_form"):
            ntimes = []
            for i in range(3):
                val = st.number_input(
                    f"Notification {i+1} (0=disable)",
                    min_value=0, max_value=1440,
                    value=(10 if i == 0 else 5),
                    key=f"notif_{i}"
                )
                if val > 0:
                    ntimes.append(val)

            submitted = st.form_submit_button("Step 3 (Click Twice)")
            if submitted:
                st.session_state["notification_times"] = ntimes
                st.session_state["step"] = 3
        st.stop()

    elif st.session_state["step"] == 3:
        st.header("Step 3: Create Classes Schedules")
        service = get_google_calendar_service()
        if service:
            st.success("You are authenticated with Google Calendar!")
            if st.button("Create Schedules (Click Once & Wait)"):
                if "df" not in st.session_state:
                    st.error("No timetable data found. Please go back to Step 1.")
                else:
                    cal_id = get_or_create_calendar(service, "WIN SEM", TIMEZONE)
                    if cal_id:
                        df = st.session_state["df"]
                        notifs = st.session_state.get("notification_times", [])

                        # Retrieve the chosen batch constraints
                        from_date = st.session_state["SEMESTER_START"]
                        until_str = st.session_state["SEMESTER_END_STR"]
                        skip_ranges = st.session_state["SKIP_RANGES"]
                        tmap = st.session_state["theory_map"]
                        lmap = st.session_state["lab_map"]

                        ok = create_calendar_events(
                            service,
                            df,
                            cal_id,
                            from_date=from_date,
                            until_str=until_str,
                            skip_ranges=skip_ranges,
                            theory_mapping=tmap,
                            lab_mapping=lmap,
                            notifications=notifs
                        )
                        if ok:
                            st.experimental_set_query_params()  # Clear query params
                            st.success("Calendar events created successfully!")
                            if "google_token" in st.session_state:
                                del st.session_state["google_token"]
                            st.info("Classes Successfully Added Into Your Google Calendar")
                            st.info("Open Google Calendar App using the same account used here.")
        else:
            st.warning("Not authenticated. Go back to Step 1 to sign in.")
        st.stop()

    else:
        st.warning("Invalid step. Resetting to Step 1.")
        st.session_state["step"] = 1
        st.stop()

if __name__ == "__main__":
    main()
