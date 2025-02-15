import streamlit as st
import re
import datetime
import pandas as pd
import os
import pickle
import csv
from io import StringIO
from datetime import datetime, timedelta

# Instead of run_local_server, we'll do a manual code flow:
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

from ics import Calendar, Event  # If you need ICS functionality

# ---------------- Disable Enter Key ----------------
st.markdown(
    """
    <script>
    document.addEventListener('keydown', function(event) {
      // Prevent default action if Enter is pressed
      if (event.key === "Enter") {
        event.preventDefault();
      }
    });
    </script>
    """,
    unsafe_allow_html=True
)

# ---------------- Google Calendar Setup ----------------
SCOPES = ["https://www.googleapis.com/auth/calendar"]

def authenticate_google_calendar_manual():
    """
    Manual OAuth flow using "urn:ietf:wg:oauth:2.0:oob" to avoid 'redirect_uri' errors.
    We open the sign-in URL in a new tab, user pastes code back in the Streamlit app.
    """
    creds = None

    # 1) Check if we already have credentials in session_state
    if "google_token" in st.session_state:
        creds = pickle.loads(st.session_state["google_token"])

    # 2) If no valid creds, do a manual code flow
    if not creds or not creds.valid:
        # If we have partial creds but they're expired, try to refresh
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                st.session_state["google_token"] = pickle.dumps(creds)
                return build("calendar", "v3", credentials=creds)
            except Exception as e:
                st.error(f"Failed to refresh token: {e}")
                return None
        else:
            # Must start a brand-new flow
            if not os.path.exists("credentials.json"):
                st.error("No credentials.json found! Please place it in the same directory.")
                return None

            flow = InstalledAppFlow.from_client_secrets_file(
                "credentials.json", SCOPES
            )
            # Explicitly set the out-of-band redirect:
            flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"

            # If we haven't generated the auth_url yet, do so now
            if "auth_url" not in st.session_state:
                # Generate the authorization URL for out-of-band
                auth_url, _ = flow.authorization_url(
                    prompt="consent",
                    access_type="offline"
                )
                st.session_state["auth_url"] = auth_url

                # Attempt to auto-open the link in a new tab:
                open_script = f"""
                <script>
                    window.open("{auth_url}", "_blank");
                </script>
                """
                st.markdown(open_script, unsafe_allow_html=True)

            # 3) Ask user to paste code
            st.info("**A new tab should have opened for Google sign-in.**")
            st.write(
                "**If it did not open automatically,** "
                f"[click here to authorize]({st.session_state['auth_url']})"
            )
            code = st.text_input("**Paste the authorization code from the new tab here**:")

            # 4) Once user pastes the code and clicks "Submit Code", we try to fetch_token
            if st.button("Submit Code"):
                if code.strip():
                    try:
                        flow.fetch_token(code=code.strip())
                        creds = flow.credentials
                        st.session_state["google_token"] = pickle.dumps(creds)
                        st.success("Authentication successful! You can proceed.")
                    except Exception as e:
                        st.error(f"Error fetching token: {e}")
                        return None
                else:
                    st.warning("Please paste the code from Google first.")
                # Stop so the user sees success/fail. Next run should have creds
                st.stop()

    if not creds:
        return None

    return build("calendar", "v3", credentials=creds)

def get_or_create_calendar(service, calendar_name, timezone="Asia/Kolkata"):
    if not service:
        return None
    result = service.calendarList().list().execute()
    for cal in result.get("items", []):
        if cal.get("summary") == calendar_name:
            return cal.get("id")
    body = {
        "summary": calendar_name,
        "timeZone": timezone,
    }
    created = service.calendars().insert(body=body).execute()
    return created.get("id")

def get_first_date_on_or_after(start_date, target_weekday):
    """Return the first date on or after start_date that falls on target_weekday (0=Monday)."""
    days_ahead = target_weekday - start_date.weekday()
    if days_ahead < 0:
        days_ahead += 7
    return start_date + timedelta(days=days_ahead)

# Example placeholders
theory_mapping = {}
lab_mapping = {}
weekday_map = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}

def create_calendar_events(service, df, semester_start_date, calendar_id,
                           timezone="Asia/Kolkata", notifications=[]):
    """Creates events from a DataFrame with columns: Course, Slot, Venue, Faculty Details."""
    if not service or not calendar_id:
        return False

    overrides = [{"method": "popup", "minutes": m} for m in notifications]
    reminders = {
        "useDefault": False,
        "overrides": overrides
    }

    total_rows = len(df)
    progress_bar = st.progress(0)
    success = True

    for idx, row in df.iterrows():
        course = row.get("Course", "").strip()
        slot_field = row.get("Slot", "").strip()
        venue = row.get("Venue", "").strip()
        faculty = row.get("Faculty Details", "").strip()

        if "EMBEDDED PROJECT" in course.upper():
            continue
        if "NIL-ONL" in venue.upper():
            continue

        summary = f"{course} [{slot_field}]"
        try:
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

            if is_lab:
                mapping = lab_mapping.get(lab_key)
                if not mapping:
                    st.warning(f"Lab slot '{lab_key}' not found in mapping. Skipping.")
                    continue
                for day_code, start_str, end_str in mapping:
                    start_hour, start_minute = map(int, start_str.split(":"))
                    end_hour, end_minute = map(int, end_str.split(":"))
                    first_date = get_first_date_on_or_after(semester_start_date, weekday_map[day_code])
                    dtstart = datetime.combine(first_date, datetime.min.time()).replace(hour=start_hour, minute=start_minute)
                    dtend = datetime.combine(first_date, datetime.min.time()).replace(hour=end_hour, minute=end_minute)
                    event = {
                        "summary": summary,
                        "location": venue,
                        "description": faculty,
                        "start": {"dateTime": dtstart.isoformat(), "timeZone": timezone},
                        "end": {"dateTime": dtend.isoformat(), "timeZone": timezone},
                        "recurrence": [f"RRULE:FREQ=WEEKLY;BYDAY={day_code}"],
                        "reminders": reminders,
                    }
                    service.events().insert(calendarId=calendar_id, body=event).execute()
            else:
                # Theory
                for tok in slot_tokens:
                    mapping = theory_mapping.get(tok)
                    if not mapping:
                        st.warning(f"Theory slot '{tok}' not found in mapping. Skipping.")
                        continue
                    for day_code, start_str, end_str in mapping:
                        start_hour, start_minute = map(int, start_str.split(":"))
                        end_hour, end_minute = map(int, end_str.split(":"))
                        first_date = get_first_date_on_or_after(semester_start_date, weekday_map[day_code])
                        dtstart = datetime.combine(first_date, datetime.min.time()).replace(hour=start_hour, minute=start_minute)
                        dtend = datetime.combine(first_date, datetime.min.time()).replace(hour=end_hour, minute=end_minute)
                        event = {
                            "summary": summary,
                            "location": venue,
                            "description": faculty,
                            "start": {"dateTime": dtstart.isoformat(), "timeZone": timezone},
                            "end": {"dateTime": dtend.isoformat(), "timeZone": timezone},
                            "recurrence": [f"RRULE:FREQ=WEEKLY;BYDAY={day_code}"],
                            "reminders": reminders,
                        }
                        service.events().insert(calendarId=calendar_id, body=event).execute()

        except Exception as e:
            st.error(f"Error creating event for {course}: {str(e)}")
            success = False

        progress_val = int(((idx + 1) / total_rows) * 100)
        progress_bar.progress(min(progress_val, 100))

    progress_bar.progress(100)
    return success

def main():
    st.title("Get Notifications on Google Calendar!!!")

    st.header("Google Authentication (OOB)")

    service = authenticate_google_calendar_manual()
    if service:
        st.success("You are authenticated! You can now create events.")
        # Example usage
        if st.button("Create Test Event"):
            calendar_id = get_or_create_calendar(service, "Academic Timetable", "Asia/Kolkata")
            if calendar_id:
                df = pd.DataFrame([{
                    "Course": "Test Course",
                    "Slot": "A1",
                    "Venue": "TestVenue-CB",
                    "Faculty Details": "Test Faculty"
                }])
                semester_start_date = datetime.now().date()
                success = create_calendar_events(
                    service,
                    df,
                    semester_start_date,
                    calendar_id,
                    timezone="Asia/Kolkata",
                    notifications=[10, 5]  # example
                )
                if success:
                    st.success("Test event created!")
    else:
        st.warning("Not authenticated. Please complete the steps above to sign in.")

if __name__ == "__main__":
    main()
