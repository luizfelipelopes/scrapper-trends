import asyncio
import time
import requests
import re
import json
import base64
import os
import random
from pathlib import Path
from datetime import datetime, time
from urllib.parse import urljoin
from google import genai
from dotenv import load_dotenv

from playwright.async_api import async_playwright

load_dotenv()


GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_API_GPT = os.getenv("GEMINI_API_GPT")
WP_URL = os.getenv("WP_BLOG_FINANCE_URL")
WP_USER = os.getenv("WP_BLOG_FINANCE_USER")
WP_PASS = os.getenv("WP_BLOG_FINANCE_PASS")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_FINANCE_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TRENDS_URL = os.getenv("TRENDS_URL_FINANCE")

async def main():

    async def get_image_element(page):
        selectors = [".content-media-container figure img", ".article__content--body figure picture img", ".body-container figure picture img", "figure img", "picture img", "section img", "article img", "div a img"]

        for selector in selectors:
            img = await page.query_selector(selector)
            if img:
                print(img)
                return img

        print("Nenhuma imagem encontrada com os seletores fornecidos.")
        return None
    
    async def dowload_cover_image(page, href):

        await page.goto(href, wait_until="domcontentloaded", timeout=120000)

        title_locator = page.locator('h1').first
        title = await title_locator.text_content()

        safe_title = re.sub(r'[^a-zA-Z0-9_\-]', '_', title)

        img_locator = await get_image_element(page)

        srcset = await img_locator.get_attribute("srcset")
        if srcset:
            largest = srcset.split(",")[-1].split()[0].strip()
            img_src = urljoin(href, largest)
        else:
            img_src = await img_locator.get_attribute('src')
            print(f'🔗 Imagem encontrada: {img_src} - Título: {safe_title}')   
            img_src = urljoin(href, img_src.replace('x240', 'x720'))  # Garante que o link da imagem seja absoluto

         

        if img_src:
            # Faz o download da imagem
            response = requests.get(img_src)
            img_path = f'covers/{safe_title}.jpg'

            if response.status_code == 200:
                with open(f'covers/{safe_title}.jpg', 'wb') as f:
                    f.write(response.content)
            else:
                print(f'Falha ao baixar a imagem: Status {response.status_code}')
        else:
            print('Nenhuma imagem encontrada.')

        return img_path, safe_title
    
    async def generate_content_ai(href, href2, href3, links_wordpress):
        
        prompt = f"""
        De acordo com com as políticas do Google Adsense, o conteúdo superficial com pouco ou nenhum valor agregado são conteúdos de baixa qualidade como por exemplo:

        Páginas afiliadas sem valor agregado:

        A afiliação sem valor agregado é a prática de publicar conteúdo com links de produtos afiliados em que as descrições e avaliações são copiadas diretamente do comerciante original, sem nenhum conteúdo original ou valor agregado.

        As páginas afiliadas podem ser consideradas sem valor agregado se fizerem parte de um programa que distribui o próprio conteúdo em uma rede de afiliados sem oferecer valor adicional. Muitas vezes, eles aparentam ser sites comuns ou criados com base em modelos que exibem conteúdo duplicado igual ou similar dentro do mesmo site ou em diversos domínios ou idiomas. Se uma página de resultados da Pesquisa retorna vários desses sites, todos com o mesmo conteúdo, as páginas afiliadas sem valor agregado tornam a experiência do usuário frustrante.

        Nem todo site que participa de um programa de afiliados é um afiliado sem valor agregado. Bons sites afiliados agregam valor oferecendo conteúdo ou recursos significativos. Exemplos de boas páginas afiliadas incluem informações adicionais sobre preço, avaliações originais de produtos, testes e classificações rigorosos, navegação em produtos ou categorias e comparações de produtos.

        Conteúdo de outras fontes, por exemplo: conteúdo copiado ou postagens de baixa qualidade em blogs de convidados (raspagem de dados): 

        A raspagem de dados se refere à prática de copiar conteúdo de outros sites, muitas vezes de forma automatizada, e hospedar esse conteúdo com o objetivo de manipular as classificações de pesquisa. Exemplos de raspagem abusiva de dados:

        Republicar conteúdo de outros sites sem adicionar conteúdo original ou valor, ou nem mesmo citar a fonte original
        Copiar conteúdo de outros sites, fazer pequenas modificações (por exemplo, usando sinônimos ou técnicas automatizadas) e publicar novamente
        Reprodução de feeds de conteúdo de outros sites sem oferecer qualquer tipo de benefício exclusivo ao usuário
        Criação de sites voltados à incorporação ou compilação de conteúdo, como vídeos, imagens ou mídias diversas de outros sites, sem acrescentar valor significativo para o usuário


        Abuso de Doorways: O abuso de doorways é quando sites ou páginas são criados para a classificação em consultas de pesquisa específicas e semelhantes. Elas levam os usuários a páginas intermediárias que não são tão úteis quanto o destino final. Exemplos de abuso de doorways incluem:

        Ter vários sites com pequenas variações no URL e na página inicial para maximizar o alcance em qualquer consulta específica
        Ter várias páginas ou muitos nomes de domínio segmentados por regiões ou cidades específicas que direcionam os usuários a uma página
        Gerar páginas para direcionar os visitantes à parte utilizável ou relevante de um site
        Criar páginas consideravelmente semelhantes e mais próximas aos resultados de pesquisa do que, por exemplo, uma hierarquia claramente definida e navegável

        Essas técnicas não oferecem aos usuários conteúdo substancialmente exclusivo ou importante, além de violar as políticas contra spam.

        Baseado nesses conceitos, crie um post para blog de noticias de finanças com conteúdo original, relevante, magnético e exclusivo. O conteúdo deve possuir um texto já em html (apenas o corpo do texto para inserir no editor do Wordpress) com retorno em formato json (apenas os campos 'title', 'slug', 'meta_description', 'keyword' e 'body'), com no minimo 600 palavras e links externos (deve possuir links de saída!). Os links internos devem vir ao final do post em tópicos com o título 'Outras noticias que podem te interessar:' e devem vir dos seguintes links: {links_wordpress}. Esses links internos devem se limitar até 4 links.
        O campo 'title' deve conter no máximo 57 caracteres.
        A keyword deve ser a mais importante do texto, e deve ser usada no título, no slug, na meta description e no corpo do texto.
        A keyword, ou sinônimos dela, devem ser exibidos em até 70% dos subtítulos H2 e H3 (não em todos os subtítulos!).
        O campo 'meta description' deve conter no mínimo 120 caracteres e no máximo 146 caracteres.
        Todo o texto deve respeitar as regras de SEO e as regras do Google Adsense, se baseando nas seguintes noticias dos links: {href} {href2} {href3}. Os links externos também devem ser baseados nesses links.
        O conteúdo html no campo 'body' deve ser inserido dentro de uma tag <article>. E dentro da tag <article> não deve conter a tag <h1> e nem deve haver sugestões para o autor do post no meio do texto. 
        O primeiro parágrafo não deve conter a tag <h2> e deve ser uma introdução ao assunto do post, sem repetir o título do post.
        A idéia é que o texto já esteja pronto para ser publicado no Wordpress.
        """

        client = genai.Client(api_key=GEMINI_API_KEY)

        response = client.models.generate_content(
            model=GEMINI_API_GPT, contents=prompt
        )

        resposta_limpa = response.text.strip('`').replace('json\n', '', 1).strip()

        return json.loads(resposta_limpa)

    async def auth_wordpress():
        username = WP_USER
        application_password = WP_PASS

        # Codifica a autenticação em Base64
        credentials = f'{username}:{application_password}'
        token = base64.b64encode(credentials.encode())
        headers_auth = {
            'Authorization': f'Basic {token.decode("utf-8")}'
        }

        return headers_auth
    
    async def upload_image_to_wordpress(headers_auth, site_url, image_path, match):
        # 1️⃣ Upload da imagem para /media
        with open(image_path, 'rb') as img:
            image_data = img.read()

        media_headers = {
            **headers_auth,
            'Content-Disposition': 'attachment; filename="imagem.jpg"',
            'Content-Type': 'image/jpeg'
        }

        post_data = {
                'title': match['title'],
                'alt_text': match['title'],
                'caption': match['title'],
                'description': match['meta_description']
                }

        media_response = requests.post(
            f'{site_url}/wp-json/wp/v2/media',
            headers=media_headers,
            data=image_data
        )

        if media_response.status_code != 201:
            print('Erro ao fazer upload da imagem:', media_response.text)

        media_id = media_response.json()['id']

        media_headers = {
            **headers_auth,
            'Content-Type': 'application/json'
        }
        meta_response = requests.put(
            f"{site_url}/wp-json/wp/v2/media/{media_id}",
            headers=media_headers,
            json=post_data
        )

        if meta_response.status_code != 200:
            print("Erro ao atualizar metadados:", meta_response.text)

        return media_id

    async def create_post_wordpress(headers_auth, site_url, media_id, match, link):
        title_terms = r'Vale Tudo|A Viagem|Guerreiros do Sol|Garota do Momento'
        # 2️⃣ Criação do post com imagem destacada
        post_data = {
            'title': match['title'],
            'content': match['body'],
            'status': 'publish',
            'featured_media': media_id,
            'slug': match['slug'],
            'yoast_description': match['meta_description'],
            'yoast_keyword': match['keyword'],
            'categories': [
                1
            ],
            'author': random.choice([2,3,4,5,6,7,8,9,10]),
        }

        post_headers = {
            **headers_auth,
            'Content-Type': 'application/json'
        }

        post_response = requests.post(
            f'{site_url}/wp-json/wp/v2/posts',
            headers=post_headers,
            json=post_data
        )

        if post_response.status_code == 201:
            post_link = post_response.json()['link']
            log_message = f'✅ Post criado com sucesso! Em {datetime.now().strftime('%d/%m/%Y %H:%M:%S')} => Link {link}: {post_link}'
            print(log_message)
        else:
            log_message = f'❌ Erro ao criar o post: {post_response.text} - Em {datetime.now().strftime('%d/%m/%Y %H:%M:%S')} => Link {link}'
            print(log_message)
        
        return log_message    

    async def search_trends(link, ref = 1):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()

            # Create a new page in the provided browser context
            page = await context.new_page()
    
            await page.goto(TRENDS_URL)
            await page.click(f'//*[@id="trend-table"]/div[1]/table/tbody[2]/tr[{link}]')
            
            href = await page.locator('.jDtQ5 a').first.get_attribute('href')
            href2 = await page.locator('.jDtQ5 a').nth(1).get_attribute('href')
            href3 = await page.locator('.jDtQ5 a').nth(2).get_attribute('href')

            await page.set_extra_http_headers({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            })

            link_image = href if ref == 1 else href2 if ref == 2 else href3

            if 'nsctotal.com.br' in link_image:
                exception_message = f'❌ Link {link} - Em {datetime.now()} - O link {link_image} não é válido. Pulando este link.'
                print(exception_message)
                await browser.close()
                raise Exception(exception_message)

            info_image_path = await dowload_cover_image(page, link_image)
            image_path, safe_title = info_image_path

            await browser.close()

            return href, href2, href3, image_path, safe_title

    def remove_image(image_path):
        img_local = os.path.join("covers", f'{image_path}.jpg')
        if os.path.exists(img_local):
            os.remove(img_local)

    async def recover_links_wordpress():
        headers_auth = await auth_wordpress()

        response = requests.get(f'{WP_URL}/wp-json/wp/v2/posts', headers=headers_auth)

        if response.status_code == 200:
            posts = response.json()
            posts_links = [post['link'] for post in posts]
        else:
            print('Erro ao recuperar links do WordPress:', response.text)
            posts_links = []        
        
        response = requests.get(f'{WP_URL}/wp-json/wp/v2/categories', headers=headers_auth)

        if response.status_code == 200:
            posts = response.json()
            categories_links = [post['link'] for post in posts]
            
        else:
            print('Erro ao recuperar links do WordPress:', response.text)
            categories_links = []     

        return posts_links + categories_links       
    
    async def recover_slugs_wordpress():
        headers_auth = await auth_wordpress()

        response = requests.get(f'{WP_URL}/wp-json/wp/v2/posts', headers=headers_auth)

        if response.status_code == 200:
            posts = response.json()
            posts_slugs = [post['slug'] for post in posts]
        else:
            print('Erro ao recuperar slugs do WordPress:', response.text)
            posts_slugs = []        
        
        return posts_slugs       

    async def send_telegram_message(log_message):
        
        url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
        payload = {
            'chat_id': TELEGRAM_CHAT_ID,
            'text': log_message
        }
        
        response = requests.post(url, data=payload)
        
        if response.status_code != 200:
            print(f'Falha ao enviar mensagem: {response.text}')

    async def run_task(image_path, safe_title, match, link):
   
        headers_auth = await auth_wordpress()

        # 1️⃣ Upload da imagem para /media
        media_id = await upload_image_to_wordpress(headers_auth, WP_URL, image_path, match)

        # 2️⃣ Criação do post com imagem destacada
        log_message = await create_post_wordpress(headers_auth, WP_URL, media_id, match, link)
        remove_image(safe_title)

        await send_telegram_message(log_message)
    
    async def load_contents():
        links_wordpress = await recover_links_wordpress()
        slugs = await recover_slugs_wordpress()

        links = []
        link = 1
        ref = 1
        tentativas = 0

        while len(links) < 10:
            try:
                    print(f'🔗 Tentando carregar o link {link}, ref: {ref} - Em {datetime.now()}')
                    href, href2, href3, image_path, safe_title = await search_trends(link, ref)
                    match = await generate_content_ai(href, href2, href3, links_wordpress)
                    
                    if match['slug'] in slugs:
                        print(f'🔗 O slug "{match["slug"]}" já existe no WordPress. Pulando este link.')
                        tentativas = 0
                        link += 1
                        ref = 1
                        
                        continue

                    links.append({
                        'link': link,
                        'href': href,
                        'href2': href2,
                        'href3': href3,
                        'image_path': image_path,
                        'safe_title': safe_title,
                        'match': match
                    })
                    link += 1
                    ref = 1

            except Exception as e:
                print(f'❌ Em {datetime.now()} - Ocorreu um erro: {e}. Tentativa {tentativas+1} de 5. Link: {link}, Ref: {ref}')
                tentativas += 1
                if tentativas == 5 and link < 25 and ref < 3:
                        tentativas = 0
                        ref += 1
                    
                if tentativas == 5 and link < 25 and ref == 3:
                    tentativas = 0
                    ref = 1    
                    link += 1

        return links
    
    async def checkComercialHour():
        agora = datetime.now().time()
        inicio = time(1,0)   # 01:00
        fim = time(5,0)      # 05:00

        print(agora, inicio, fim)

        if inicio <= agora <= fim:
            print("❌ Estamos entre 01:00 e 05:00.")
            return False
        else:
            return True
    
    indexLinks = 0
    links = []
    while True:

        isComercialHour = await checkComercialHour()
        print(f'🔗 isComercialHour: {isComercialHour} - Em {datetime.now()}')
        if not isComercialHour:
            print(f'❌ while: O script não será executado entre 01:00 e 05:00. Em {datetime.now()}')
            await asyncio.sleep(30)
            continue
        
        if indexLinks >= len(links):
            indexLinks = 0
            print(f'🔗 Reiniciando o loop de links - Em {datetime.now()}')
            links = await load_contents()
        

        for link_info in links:
            tentativas = 0
            
            path = Path(link_info['image_path'])
            if not path.exists():
                print(f'❌ A imagem {link_info["image_path"]} não existe. Tentativa {tentativas+1} de 5. Link: {link_info["link"]}')
                indexLinks += 1
                continue
            
            while tentativas < 5:
                try:
                    
                    isComercialHour = await checkComercialHour()
                    if not isComercialHour:
                        print(f'❌ try/catch: O script não será executado entre 01:00 e 05:00. Em {datetime.now()}')
                        break

                    print(f'🔗 Executando a tarefa para o link {indexLinks+1} de {len(links)} - Em {datetime.now()} - Imagem: {link_info['image_path']}')
                    await run_task(link_info['image_path'] , link_info['safe_title'] , link_info['match'] , link_info['link'])
                    indexLinks += 1
                    
                    await asyncio.sleep(4*60*60)  # 4*60*60 segundos = 240 min (4hrs)
                    # await asyncio.sleep(60*60)  # 3600 segundos = 60 min
                    # await asyncio.sleep(30)  # 1800 segundos = 30 min
                    break # Se a tarefa for executada com sucesso, sai do loop de tentativas
                except Exception as e:
                    print(f'❌ run_task(): Em {datetime.now()} - Ocorreu um erro: {e}. Tentativa {tentativas+1} de 5. Link: {link_info['link']}')
                    tentativas += 1


if __name__ == "__main__":
    asyncio.run(main())
