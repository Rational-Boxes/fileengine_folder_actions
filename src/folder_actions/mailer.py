# Copyright (C) 2026 James Hickman
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""SMTP delivery for the digest (SPECIFICATION §11c). Best-effort — a send failure
returns False (the sender leaves the period unmarked for retry) and never raises.
Same SMTP settings shape ldap_manager uses for invite/reset mail.
"""
from __future__ import annotations

import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger("folder_actions.mailer")


class SmtpMailer:
    def __init__(self, config):
        self.config = config

    @property
    def configured(self) -> bool:
        return bool(self.config.smtp_host and self.config.digest_from)

    def send(self, to_addr: str, subject: str, text: str, html: str) -> bool:
        if not self.configured or not to_addr:
            return False
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.config.digest_from
        msg["To"] = to_addr
        msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
        try:
            import smtplib
            with smtplib.SMTP(self.config.smtp_host, self.config.smtp_port, timeout=15) as s:
                try:
                    s.starttls()
                except Exception:
                    pass  # server without STARTTLS
                if self.config.smtp_user:
                    s.login(self.config.smtp_user, self.config.smtp_password)
                s.sendmail(self.config.digest_from, [to_addr], msg.as_string())
            return True
        except Exception:
            log.warning("digest send to %s failed", to_addr, exc_info=True)
            return False
