"""
FASS Network — vendor team agreement generation.

This is the deliberately small slice of "contract lifecycle management" we're
actually building right now: a real, downloadable subcontractor agreement PDF
prefilled with the vendor + opportunity details already on file in
network_vendors / proposals. There is no e-signature integration here — the
frontend (Network.jsx) tracks "sent" vs "signed" manually via
vendor_contracts.status, the same honor-system pattern this whole app uses for
billing. True e-signature/redlining (à la Docusign/Intellistack) is real,
sensitive, compliance-bearing scope and is intentionally deferred, not built
into this pass.
"""
import io
from datetime import date

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib import colors

router = APIRouter(prefix="/network", tags=["network"])


class SubcontractorAgreementRequest(BaseModel):
    vendor_name: str
    vendor_contact_email: str
    trade_category: str
    proposal_title: str
    proposal_agency: str = ""
    role: str = ""
    company_name: str = "FASS Flow / FASS Muffin Operating Systems"


@router.post("/subcontractor-agreement")
async def generate_subcontractor_agreement(body: SubcontractorAgreementRequest):
    if not body.vendor_name.strip() or not body.proposal_title.strip():
        raise HTTPException(status_code=400, detail="vendor_name and proposal_title are required")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        topMargin=0.85 * inch, bottomMargin=0.85 * inch,
        leftMargin=0.9 * inch, rightMargin=0.9 * inch,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("AgreementTitle", parent=styles["Title"], fontSize=18, spaceAfter=6)
    sub_style = ParagraphStyle("AgreementSub", parent=styles["Normal"], fontSize=9, textColor=colors.gray, spaceAfter=18)
    h2 = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=12, spaceBefore=14, spaceAfter=6)
    body_style = ParagraphStyle("Body", parent=styles["Normal"], fontSize=10, leading=15)

    today = date.today().strftime("%B %d, %Y")
    role = body.role.strip() or body.trade_category

    story = [
        Paragraph("Subcontractor / Vendor Agreement", title_style),
        Paragraph(f"Generated {today} via FASS Flow", sub_style),
        Paragraph("Parties", h2),
        Table(
            [
                ["Prime Contractor:", body.company_name],
                ["Subcontractor / Vendor:", body.vendor_name],
                ["Vendor Contact:", body.vendor_contact_email],
                ["Trade / Category:", body.trade_category],
                ["Role on this Opportunity:", role],
            ],
            colWidths=[1.9 * inch, 4.1 * inch],
        ),
        Paragraph("Opportunity", h2),
        Paragraph(
            f"<b>{body.proposal_title}</b>"
            + (f" — {body.proposal_agency}" if body.proposal_agency else ""),
            body_style,
        ),
        Paragraph("Scope of Work", h2),
        Paragraph(
            f"Subcontractor agrees to perform {role} work in support of the Prime Contractor's "
            f"delivery of the above opportunity, in accordance with the solicitation's statement "
            f"of work and any task order issued under it. Detailed scope, schedule, and pricing for "
            f"this specific engagement will be set out in a signed task order or purchase order "
            f"referencing this Agreement.",
            body_style,
        ),
        Paragraph("Compliance", h2),
        Paragraph(
            "Subcontractor represents that it is, and will remain for the duration of performance, "
            "in good standing for any registration, licensing, bonding, insurance, or certification "
            "required to perform the scope above, and will provide documentation of the same upon "
            "request by the Prime Contractor.",
            body_style,
        ),
        Paragraph("Confidentiality", h2),
        Paragraph(
            "Each party agrees to keep confidential any non-public information of the other party "
            "disclosed in connection with this opportunity, and to use it solely to perform the work "
            "described above.",
            body_style,
        ),
        Paragraph("Term", h2),
        Paragraph(
            "This Agreement governs the relationship between the parties for the above opportunity "
            "and remains in effect through completion of the associated period of performance, unless "
            "terminated earlier in writing by either party.",
            body_style,
        ),
        Spacer(1, 28),
        Paragraph("Signatures", h2),
        Table(
            [
                ["Prime Contractor", "Subcontractor / Vendor"],
                ["", ""],
                ["Signature: ____________________________", "Signature: ____________________________"],
                ["Date: _______________", "Date: _______________"],
            ],
            colWidths=[3 * inch, 3 * inch],
            style=TableStyle([
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ]),
        ),
    ]

    doc.build(story)
    buf.seek(0)
    filename = f"Subcontractor-Agreement-{body.vendor_name.replace(' ', '-')}.pdf"
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
