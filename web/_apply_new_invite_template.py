import re

path = r"G:\My Drive\CHRIS Folder\Claude Folder\Real Estate\realestate-skills\web\admin.html"
with open(path, encoding="utf-8") as f:
    content = f.read()

start_marker = "      const htmlBody = `<!DOCTYPE"
end_marker = "      this.invitePreviewHtml = readableHtmlBody;\n    },"

start_idx = content.index(start_marker)
end_idx = content.index(end_marker) + len(end_marker)
old_block = content[start_idx:end_idx]

new_block = r'''      const inviteTeaser = esc(copy.subhead || copy.headline);
      const heroHeadline = esc(copy.headline).replace(/\n/g, '<br>');
      const heroSubhead = esc(copy.subhead).replace(/\n/g, '<br>');
      const checklist = [
        'Property score &amp; suggested offer', '3-scenario cash flow',
        'Cap rate, cash-on-cash, DSCR', 'Neighborhood &amp; comp data',
        'Buy &amp; Hold / BRRRR / Fix &amp; Flip', 'Financing options',
      ];
      const checklistRows = [0, 2, 4].map(i => `<tr>
        <td class="stack" width="50%" valign="top" style="width:50%;padding:5px 10px 5px 0;color:#20404F;font-size:13.5px;line-height:1.45;"><span style="color:#1596D6;font-weight:bold;">&#10003;</span>&nbsp; ${checklist[i]}</td>
        <td class="stack" width="50%" valign="top" style="width:50%;padding:5px 0 5px 10px;color:#20404F;font-size:13.5px;line-height:1.45;"><span style="color:#1596D6;font-weight:bold;">&#10003;</span>&nbsp; ${checklist[i + 1]}</td>
      </tr>`).join('');
      const benefitRows = copy.benefits.map((benefit, index) => `<tr><td style="padding:${index === 0 ? '0' : '16px 0 0'};">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
          <td width="34" valign="top" style="width:34px;color:#28C5FF;font-size:22px;font-weight:bold;line-height:1;">0${index + 1}</td>
          <td valign="top" style="color:#20404F;font-size:13.5px;line-height:1.5;"><strong style="color:#0B2733;font-size:14.5px;">${esc(benefit.title)}.</strong> ${esc(benefit.text)}</td>
        </tr></table>
      </td></tr>`).join('');
      const htmlBody = `<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>${esc(copy.subject)}</title>
<style>@media only screen and (max-width:620px){.px{padding-left:22px !important;padding-right:22px !important;}.h1{font-size:26px !important;line-height:1.2 !important;}.stack{display:block !important;width:100% !important;}.cta a{display:block !important;}}</style>
</head>
<body style="margin:0;padding:0;background:#E7EDF1;font-family:Arial,Helvetica,sans-serif;">
<span style="display:none;font-size:1px;color:#E7EDF1;line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;">${inviteTeaser}</span>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#E7EDF1;"><tr><td align="center" style="padding:28px 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:600px;background:#FFFFFF;border-radius:10px;overflow:hidden;box-shadow:0 2px 10px rgba(11,39,51,0.10);">

  <tr><td class="px" style="padding:22px 32px 20px;background:#FFFFFF;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
      <td width="300" align="left" valign="middle" style="width:300px;">${logo}</td>
      <td width="236" align="right" valign="middle" style="width:236px;color:#8AA3B0;font-size:10px;letter-spacing:1px;text-transform:uppercase;">In partnership with<br><span style="color:#1596D6;font-weight:bold;font-size:13px;letter-spacing:0;text-transform:none;">PropYield.AI</span></td>
    </tr></table>
  </td></tr>

  <tr><td class="px" style="padding:34px 32px 32px;background:#0B2733;">
    <div style="color:#28C5FF;font-size:11px;font-weight:bold;letter-spacing:2.2px;text-transform:uppercase;">${esc(copy.eyebrow)}</div>
    <div class="h1" style="color:#FFFFFF;font-size:31px;font-weight:bold;line-height:1.15;padding:12px 0 0;">${heroHeadline}</div>
    <div style="color:#B7CCD8;font-size:15px;line-height:1.55;padding:14px 0 0;">${heroSubhead}</div>
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" class="cta" style="margin-top:26px;"><tr>
      <td align="center" bgcolor="#28C5FF" style="background:#28C5FF;border-radius:6px;"><a href="${esc(loginUrl)}" style="display:block;padding:15px 34px;color:#062430;font-size:15px;font-weight:bold;text-decoration:none;">Log in and run your first property &rarr;</a></td>
    </tr></table>
  </td></tr>

  <tr><td class="px" style="padding:26px 32px 0;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border:1px solid #CFDDE5;border-radius:8px;overflow:hidden;">
      <tr><td bgcolor="#F2F8FC" style="background:#F2F8FC;padding:11px 20px;border-bottom:1px solid #CFDDE5;color:#0B2733;font-size:11px;font-weight:bold;letter-spacing:1.4px;text-transform:uppercase;">Your sign-in details</td></tr>
      <tr><td style="padding:16px 20px 18px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="font-size:13px;line-height:1.5;">
          <tr><td width="120" style="width:120px;padding:4px 0;color:#6E8794;">Login</td><td style="padding:4px 0;"><a href="${esc(loginUrl)}" style="color:#1069A8;font-weight:bold;text-decoration:underline;">${esc(loginUrl.replace(/^https?:\/\//, ''))}</a></td></tr>
          <tr><td width="120" style="width:120px;padding:4px 0;color:#6E8794;">Email</td><td style="padding:4px 0;color:#0B2733;font-weight:bold;">${agentEmail}</td></tr>
          <tr><td width="120" style="width:120px;padding:4px 0;color:#6E8794;">Temp password</td><td style="padding:4px 0;color:#0B2733;font-weight:bold;font-family:'Courier New',Courier,monospace;font-size:14px;">${tempPassword}</td></tr>
        </table>
        <div style="color:#6E8794;font-size:11.5px;line-height:1.45;padding-top:12px;border-top:1px solid #E6EEF3;margin-top:12px;">${esc(copy.cta)}</div>
      </td></tr>
    </table>
  </td></tr>

  <tr><td class="px" style="padding:28px 32px 4px;color:#20404F;font-size:15px;line-height:1.6;">
    <p style="margin:0 0 14px;">Hi ${agentName},</p>
    <p style="margin:0 0 14px;">${esc(copy.intro)}</p>
    <p style="margin:0;">${esc(copy.attachment_line)}</p>
  </td></tr>

  <tr><td class="px" style="padding:16px 32px 4px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">${checklistRows}</table>
  </td></tr>

  <tr><td class="px" style="padding:26px 32px 0;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-top:1px solid #E6EEF3;"><tr><td style="padding:18px 0 0;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">${benefitRows}</table>
    </td></tr></table>
  </td></tr>

  <tr><td class="px" align="center" style="padding:30px 32px 34px;">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" class="cta" style="margin:0 auto;"><tr>
      <td align="center" bgcolor="#0B2733" style="background:#0B2733;border-radius:6px;"><a href="${esc(loginUrl)}" style="display:block;padding:15px 40px;color:#FFFFFF;font-size:15px;font-weight:bold;text-decoration:none;">Log In Now</a></td>
    </tr></table>
  </td></tr>

  <tr><td class="px" bgcolor="#0B2733" style="background:#0B2733;padding:24px 32px;color:#9FB8C6;font-size:12px;line-height:1.6;">
    <div style="color:#FFFFFF;font-size:13px;font-weight:bold;">${contactName}</div>
    <div>${partnerName}</div>
    <div><a href="mailto:${contactEmail}" style="color:#28C5FF;text-decoration:none;">${contactEmail}</a></div>
    <div style="color:#6E8794;font-size:10px;line-height:1.6;padding-top:16px;border-top:1px solid #1C3F4F;margin-top:16px;">Powered by <span style="color:#28C5FF;font-weight:bold;">PropYield.AI</span> &middot; Questions? <a href="mailto:support@propmind.ai" style="color:#28C5FF;text-decoration:none;">support@propmind.ai</a></div>
  </td></tr>

</table>
</td></tr></table>
</body></html>`;
      this.inviteText = `Subject: ${copy.subject}\n\nHi ${this.inviteForm.agent_name || 'Agent Name'},\n\n${copy.intro}\n\n${copy.attachment_line}\n\nYour PropYield.AI Account Is Ready\nLogin URL: ${loginUrl}\nEmail: ${this.inviteForm.agent_email || 'agent@company.com'}\nTemporary Password: ${this.inviteForm.temp_password}\n\n${copy.cta}\n\n${this.tenant?.contact_name || 'Your local team'}\n${this.tenant?.company_name || 'Your Team'}`;
      this.invitePreviewHtml = htmlBody;
    },'''

if old_block not in content:
    raise SystemExit("MARKER_NOT_FOUND")

content = content.replace(old_block, new_block)
with open(path, "w", encoding="utf-8") as f:
    f.write(content)
print("replaced_ok")
